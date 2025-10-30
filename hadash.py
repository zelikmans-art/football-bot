import os
import re
import time
import requests
from datetime import datetime
import pytz

# ================== ENV VARS ==================
BETSAPI_TOKEN      = os.getenv("BETSAPI_TOKEN", "").strip()

TELEGRAM_TOKEN_1   = os.getenv("TELEGRAM_TOKEN_1", "").strip()
TELEGRAM_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1", "").strip()

TELEGRAM_TOKEN_2   = os.getenv("TELEGRAM_TOKEN_2", "").strip()
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()

# ================== CONFIG ==================
TZ                 = pytz.timezone("Asia/Jerusalem")
BET_BASE           = "https://api.betsapi.com/v1/bet365"
TIMEOUT            = 20

SCAN_INTERVAL_DAY  = 30        # seconds (09:00–23:30)
SCAN_INTERVAL_NGT  = 480       # seconds (23:31–08:59)
MAX_MINUTE         = 65
HEARTBEAT_SEC      = 10 * 60 * 60  # every 10h

# Alert thresholds
SOT_MIN            = 6
SHOTS_MIN          = 12
CORNERS_MIN        = 8
XG_MIN             = 1.0
KPASS_MIN          = 6

sent_alerts = set()

# ================== TELEGRAM ==================
def send_telegram(text: str):
    targets = []
    if TELEGRAM_TOKEN_1 and TELEGRAM_CHAT_ID_1:
        targets.append((TELEGRAM_TOKEN_1, TELEGRAM_CHAT_ID_1))
    if TELEGRAM_TOKEN_2 and TELEGRAM_CHAT_ID_2:
        targets.append((TELEGRAM_TOKEN_2, TELEGRAM_CHAT_ID_2))

    for token, chat_id in targets:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {"chat_id": chat_id, "text": text}
            r = requests.post(url, data=data, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Telegram {chat_id} error {r.status_code}: {r.text[:150]}")
            else:
                print(f"✅ Telegram sent to {chat_id}")
        except Exception as e:
            print("⚠️ Telegram exception:", e)

# ================== HELPERS ==================
def parse_minute(val) -> int:
    if val is None: return 0
    s = str(val)
    m = re.match(r"^\s*(\d+)", s)
    return int(m.group(1)) if m else 0

def to_int(x, default=0):
    try: return int(float(x))
    except: return default

def to_float(x, default=0.0):
    try: return float(x)
    except: return default

def first_nonempty(*vals):
    for v in vals:
        if v not in (None, "", "None"):
            return v
    return None

def match_header(country, league, home, away):
    return f"{country or '—'} — {league or '—'}\n{home or 'Home'} vs {away or 'Away'}"

def flatten_results(results):
    out = []
    if isinstance(results, list):
        for block in results:
            if isinstance(block, list): out.extend(block)
            elif isinstance(block, dict): out.append(block)
    elif isinstance(results, dict):
        out.append(results)
    return out

def fetch_inplay():
    url = f"{BET_BASE}/inplay"
    params = {"sport_id": 1, "token": BETSAPI_TOKEN}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    js = r.json()
    if js.get("success") == 1:
        return js.get("results", [])
    print("⚠️ inplay error:", js)
    return []

def fetch_event_stats(fi_id: str):
    url = f"{BET_BASE}/event"
    params = {"FI": fi_id, "stats": 1, "token": BETSAPI_TOKEN}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    js = r.json()
    if js.get("success") == 1:
        return js.get("results")
    return None

def extract_team_stats(stats_payload):
    if isinstance(stats_payload, list) and len(stats_payload) >= 2:
        home, away = stats_payload[0], stats_payload[1]
    elif isinstance(stats_payload, dict) and "stats" in stats_payload:
        lst = stats_payload["stats"]
        if isinstance(lst, list) and len(lst) >= 2:
            home, away = lst[0], lst[1]
        else:
            return None, None
    else:
        return None, None

    def get_val(d, keys, conv=to_int, default=0):
        for k in keys:
            if k in d: return conv(d.get(k, default))
        return default

    H = {
        "sot":      get_val(home, ["S1"]),
        "shots":    get_val(home, ["S1"]) + get_val(home, ["S2"]),
        "corners":  get_val(home, ["S10", "S11", "Corners"], default=0),
        "xg":       get_val(home, ["XG", "xg"], conv=to_float, default=0.0),
        "kpass":    get_val(home, ["KP", "KeyPasses"], default=0),
        "yellows":  get_val(home, ["YC", "Y"], default=0),
        "has_red":  get_val(home, ["RC", "R"], default=0) > 0,
        "goals":    get_val(home, ["SC"], default=0),
    }
    A = {
        "sot":      get_val(away, ["S1"]),
        "shots":    get_val(away, ["S1"]) + get_val(away, ["S2"]),
        "corners":  get_val(away, ["S10", "S11", "Corners"], default=0),
        "xg":       get_val(away, ["XG", "xg"], conv=to_float, default=0.0),
        "kpass":    get_val(away, ["KP", "KeyPasses"], default=0),
        "yellows":  get_val(away, ["YC", "Y"], default=0),
        "has_red":  get_val(away, ["RC", "R"], default=0) > 0,
        "goals":    get_val(away, ["SC"], default=0),
    }
    return H, A

def check_and_alert(ev, minute, header, fi_id, H, A):
    alerts = []

    if H["sot"] >= 6 and H["goals"] == 0:
        alerts.append(("sot_home", f"🎯 {header}\nHome 6+ shots on target, no goals."))
    if A["sot"] >= 6 and A["goals"] == 0:
        alerts.append(("sot_away", f"🎯 {header}\nAway 6+ shots on target, no goals."))

    if H["shots"] >= 12 and H["goals"] == 0:
        alerts.append(("shots_home", f"🔥 {header}\nHome 12+ total shots, no goals."))
    if A["shots"] >= 12 and A["goals"] == 0:
        alerts.append(("shots_away", f"🔥 {header}\nAway 12+ total shots, no goals."))

    if H["corners"] >= 8 and H["goals"] == 0:
        alerts.append(("corners_home", f"🏁 {header}\nHome 8+ corners, no goals."))
    if A["corners"] >= 8 and A["goals"] == 0:
        alerts.append(("corners_away", f"🏁 {header}\nAway 8+ corners, no goals."))

    if H["xg"] >= 1.0 and H["goals"] == 0:
        alerts.append(("xg_home", f"📊 {header}\nHome xG≥1.0, no goals."))
    if A["xg"] >= 1.0 and A["goals"] == 0:
        alerts.append(("xg_away", f"📊 {header}\nAway xG≥1.0, no goals."))

    if H["kpass"] >= 6 and H["goals"] == 0:
        alerts.append(("kpass_home", f"🎯 {header}\nHome 6+ key passes, no goals."))
    if A["kpass"] >= 6 and A["goals"] == 0:
        alerts.append(("kpass_away", f"🎯 {header}\nAway 6+ key passes, no goals."))

    if H["has_red"]:
        alerts.append(("red_home", f"🟥 {header}\nRed card for Home."))
    if A["has_red"]:
        alerts.append(("red_away", f"🟥 {header}\nRed card for Away."))

    if minute == 45 and (H["yellows"] + A["yellows"] == 0):
        alerts.append(("no_yellows", f"⚠️ {header}\n45': No yellow cards so far."))

    for tag, msg in alerts:
        key = f"{fi_id}:{tag}"
        if key not in sent_alerts:
            send_telegram(msg)
            print("🔔", msg.replace("\n", " | "))
            sent_alerts.add(key)

# ================== MAIN LOOP ==================
def main():
    if not BETSAPI_TOKEN:
        print("🚫 Missing BETSAPI_TOKEN env var")
        return

    print("🚀 Script started successfully — waiting for first scan...")
    last_hb = time.time()

    while True:
        now = datetime.now(TZ)
        day_mode = (9 <= now.hour < 23) or (now.hour == 23 and now.minute <= 30)
        interval = SCAN_INTERVAL_DAY if day_mode else SCAN_INTERVAL_NGT

        print(f"\n🔄 Scan @ {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | interval={interval}s")

        try:
            res = fetch_inplay()
            evs = flatten_results(res)
            print(f"📊 INPLAY rows: {len(evs)}")

            for ev in evs:
                if not isinstance(ev, dict) or ev.get("type") != "EV":
                    continue

                fi_id = ev.get("ID")
                if not fi_id:
                    continue

                home = first_nonempty(ev.get("HOME"), ev.get("T1"))
                away = first_nonempty(ev.get("AWAY"), ev.get("T2"))
                score = first_nonempty(ev.get("SS")) or "?-?"
                minute = parse_minute(first_nonempty(ev.get("TM"), ev.get("time")))
                country = first_nonempty(ev.get("CN"))
                league = first_nonempty(ev.get("CL"))

                if minute > MAX_MINUTE:
                    continue

                header = match_header(country, league, home, away)
                details = fetch_event_stats(fi_id)
                if not details:
                    continue

                stats_payload = None
                if isinstance(details, list):
                    if len(details) > 0 and isinstance(details[0], dict) and "stats" in details[0]:
                        stats_payload = details[0]["stats"]
                    else:
                        stats_payload = details
                elif isinstance(details, dict):
                    stats_payload = details.get("stats") or details

                H, A = extract_team_stats(stats_payload)
                if not H or not A:
                    continue

                check_and_alert(ev, minute, header, fi_id, H, A)

        except Exception as e:
            print("❌ Exception:", e)

        if time.time() - last_hb >= HEARTBEAT_SEC:
            send_telegram("✅ Heartbeat — bot is running")
            last_hb = time.time()

        time.sleep(interval)

if __name__ == "__main__":
    main()
