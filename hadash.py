import os
import re
import time
import requests
from datetime import datetime
import pytz

# ========== ENV VARS ==========
BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()
TELEGRAM_TOKEN_1 = os.getenv("TELEGRAM_TOKEN_1", "").strip()
TELEGRAM_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1", "").strip()
TELEGRAM_TOKEN_2 = os.getenv("TELEGRAM_TOKEN_2", "").strip()
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()

# ========== CONFIG ==========
TZ = pytz.timezone("Asia/Jerusalem")
BET_BASE = "https://api.betsapi.com/v1/bet365"
TIMEOUT = 20
SCAN_INTERVAL_DAY = 30
SCAN_INTERVAL_NGT = 480
MAX_MINUTE = 65
HEARTBEAT_SEC = 10 * 60 * 60

# Alert thresholds
SOT_MIN = 6
SHOTS_MIN = 12
CORNERS_MIN = 8
XG_MIN = 1.0
KPASS_MIN = 6

# Filter lists
EXCLUDE_WORDS = [
    "eSoccer", "Virtual", "Simulated", "FIFA", "Rocket League",
    "CS:", "Dota", "Valorant", "NBA", "Basketball", "Tennis", "Ping Pong"
]

sent_alerts = set()

# ========== TELEGRAM ==========
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
            if r.status_code == 200:
                print(f"✅ Telegram sent to {chat_id}")
            else:
                print(f"⚠️ Telegram error {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print("⚠️ Telegram exception:", e)

# ========== HELPERS ==========
def parse_minute(val):
    if val is None:
        return 0
    s = str(val)
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else 0

def to_int(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default

def to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def first_nonempty(*vals):
    for v in vals:
        if v not in (None, "", "None"):
            return v
    return None

def flatten_results(results):
    out = []
    if isinstance(results, list):
        for block in results:
            if isinstance(block, list):
                out.extend(block)
            elif isinstance(block, dict):
                out.append(block)
    elif isinstance(results, dict):
        out.append(results)
    return out

def match_header(country, league, home, away):
    return f"{country or '—'} — {league or '—'}\n{home or 'Home'} vs {away or 'Away'}"

# ========== FETCH ==========
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
    else:
        print(f"⚠️ event error for FI={fi_id}: {js}")
    return None

# ========== STAT EXTRACTION ==========
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

    def get_xg(d): return to_float(d.get("XG", 0.0))
    def get_yellow(d): return to_int(d.get("YC", 0))
    def has_red(d): return to_int(d.get("RC", 0)) >= 1
    def get_kp(d): return to_int(d.get("KP", 0))
    def get_corners(d): return to_int(d.get("S10", 0))
    def get_sot(d): return to_int(d.get("S1", 0))
    def get_shots(d): return get_sot(d) + to_int(d.get("S2", 0))

    H = {
        "sot": get_sot(home),
        "shots": get_shots(home),
        "corners": get_corners(home),
        "xg": get_xg(home),
        "kpass": get_kp(home),
        "yellows": get_yellow(home),
        "has_red": has_red(home),
        "goals": to_int(home.get("SC", 0)),
    }
    A = {
        "sot": get_sot(away),
        "shots": get_shots(away),
        "corners": get_corners(away),
        "xg": get_xg(away),
        "kpass": get_kp(away),
        "yellows": get_yellow(away),
        "has_red": has_red(away),
        "goals": to_int(away.get("SC", 0)),
    }
    return H, A

# ========== ALERTS ==========
def check_and_alert(ev, minute, header, fi_id, H, A):
    alerts = []

    if H["sot"] >= SOT_MIN and H["goals"] == 0:
        alerts.append(("sot_home", f"🎯 {header}\nHome {H['sot']} SOT, no goals"))
    if A["sot"] >= SOT_MIN and A["goals"] == 0:
        alerts.append(("sot_away", f"🎯 {header}\nAway {A['sot']} SOT, no goals"))

    if H["shots"] >= SHOTS_MIN and H["goals"] == 0:
        alerts.append(("shots_home", f"🔥 {header}\nHome {H['shots']} shots, no goals"))
    if A["shots"] >= SHOTS_MIN and A["goals"] == 0:
        alerts.append(("shots_away", f"🔥 {header}\nAway {A['shots']} shots, no goals"))

    if H["corners"] >= CORNERS_MIN and H["goals"] == 0:
        alerts.append(("corners_home", f"🏁 {header}\nHome {H['corners']} corners, no goals"))
    if A["corners"] >= CORNERS_MIN and A["goals"] == 0:
        alerts.append(("corners_away", f"🏁 {header}\nAway {A['corners']} corners, no goals"))

    if H["xg"] >= XG_MIN and H["goals"] == 0:
        alerts.append(("xg_home", f"📊 {header}\nHome xG={H['xg']}, no goals"))
    if A["xg"] >= XG_MIN and A["goals"] == 0:
        alerts.append(("xg_away", f"📊 {header}\nAway xG={A['xg']}, no goals"))

    if H["kpass"] >= KPASS_MIN and H["goals"] == 0:
        alerts.append(("kp_home", f"🎯 {header}\nHome {H['kpass']} key passes, no goals"))
    if A["kpass"] >= KPASS_MIN and A["goals"] == 0:
        alerts.append(("kp_away", f"🎯 {header}\nAway {A['kpass']} key passes, no goals"))

    if H["has_red"]:
        alerts.append(("red_home", f"🟥 {header}\nRed card (Home)"))
    if A["has_red"]:
        alerts.append(("red_away", f"🟥 {header}\nRed card (Away)"))

    if minute == 45 and (H["yellows"] + A["yellows"] == 0):
        alerts.append(("ht_noyellow", f"⚠️ {header}\n45': No yellow cards."))

    for tag, msg in alerts:
        key = f"{fi_id}:{tag}"
        if key not in sent_alerts:
            send_telegram(msg)
            print("🔔", msg.replace("\n", " | "))
            sent_alerts.add(key)

# ========== MAIN ==========
def main():
    if not BETSAPI_TOKEN:
        print("🚫 Missing BETSAPI_TOKEN")
        return

    print(f"🔑 BETSAPI token loaded: {BETSAPI_TOKEN[:4]}…{BETSAPI_TOKEN[-4:]}")
    print(f"📬 Telegram targets: {[TELEGRAM_CHAT_ID_1, TELEGRAM_CHAT_ID_2]}")
    send_telegram("✅ Football monitor started successfully")
    last_hb = time.time()

    while True:
        now = datetime.now(TZ)
        day_mode = (9 <= now.hour < 23) or (now.hour == 23 and now.minute <= 30)
        interval = SCAN_INTERVAL_DAY if day_mode else SCAN_INTERVAL_NGT

        print(f"\n🔄 Scan @ {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | interval={interval}s")

        try:
            res = fetch_inplay()
            evs = flatten_results(res)
            if not evs:
                print("ℹ️ No matches in play.")
                time.sleep(interval)
                continue

            filtered = []
            for ev in evs:
                if ev.get("type") != "EV":
                    continue
                name = (ev.get("NA") or "").lower()
                if any(w.lower() in name for w in EXCLUDE_WORDS):
                    continue
                filtered.append(ev)

            print(f"📊 {len(filtered)} real football matches after filtering.")

            for ev in filtered:
                fi_id = ev.get("ID")
                minute = parse_minute(ev.get("TM"))
                if minute > MAX_MINUTE:
                    continue

                home = ev.get("T1")
                away = ev.get("T2")
                league = ev.get("CT")
                country = ev.get("CB")
                header = match_header(country, league, home, away)
                print(f"⚽ {header} | {minute}'")

                stats = fetch_event_stats(fi_id)
                if not stats:
                    continue

                H, A = extract_team_stats(stats)
                if H and A:
                    check_and_alert(ev, minute, header, fi_id, H, A)

        except Exception as e:
            print("❌ Exception:", e)

        if time.time() - last_hb >= HEARTBEAT_SEC:
            send_telegram("💓 Heartbeat — bot running OK")
            last_hb = time.time()

        time.sleep(interval)

if __name__ == "__main__":
    main()
