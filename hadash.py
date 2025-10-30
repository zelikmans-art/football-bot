import os
import re
import time
import requests
from datetime import datetime
import pytz

# ================== ENV VARS (no secrets in code) ==================
BETSAPI_TOKEN      = os.getenv("BETSAPI_TOKEN", "").strip()

TELEGRAM_TOKEN_1   = os.getenv("TELEGRAM_TOKEN_1", "").strip()
TELEGRAM_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1", "").strip()

TELEGRAM_TOKEN_2   = os.getenv("TELEGRAM_TOKEN_2", "").strip()
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()

# ================== CONFIG ==================
TZ                 = pytz.timezone("Asia/Jerusalem")
BET_BASE           = "https://api.betsapi.com/v1/bet365"
TIMEOUT            = 20

# בקשה שלך: סריקה אחידה כל 120 שניות
SCAN_INTERVAL_DAY  = 120       # seconds (09:00–23:30)
SCAN_INTERVAL_NGT  = 120       # seconds (23:31–08:59)
MAX_MINUTE         = 65        # לא מתריעים אחרי דקה 65
HEARTBEAT_SEC      = 10 * 60 * 60  # 10h

# Alert thresholds (מה שביקשת בעבר – משאירים)
SOT_MIN            = 6
SHOTS_MIN          = 12
CORNERS_MIN        = 8
XG_MIN             = 1.0
KPASS_MIN          = 6

# מניעת כפילויות
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
                print(f"⚠️ Telegram {chat_id} error {r.status_code}: {r.text[:180]}")
            else:
                print(f"✅ Telegram sent to {chat_id}")
        except Exception as e:
            print("⚠️ Telegram exception:", e)

# ================== HELPERS ==================
VIRTUAL_PAT = re.compile(
    r"(?:^|\b)(e-?soccer|esoccer|virtual|fifa|pes|rocket\s*league|simulated)(?:\b|$)",
    flags=re.IGNORECASE
)
def is_virtual_or_esports(name: str, comp: str) -> bool:
    name = name or ""
    comp = comp or ""
    return bool(VIRTUAL_PAT.search(name)) or bool(VIRTUAL_PAT.search(comp))

def parse_minute(val) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    s = str(val)
    m = re.match(r"^\s*(\d+)", s)
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

def match_header(country, league, home, away):
    cn = country or "—"
    lg = league or "—"
    h  = home or "Home"
    a  = away or "Away"
    return f"{cn} — {lg}\n{h} vs {a}"

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

# ================== BETSAPI ==================
def fetch_inplay():
    url = f"{BET_BASE}/inplay"
    params = {"sport_id": 1, "token": BETSAPI_TOKEN}  # 1 = Soccer
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        print(f"🌐 GET {r.url} | status={r.status_code}")
        js = r.json()
        if js.get("success") == 1:
            return js.get("results", [])
        else:
            print("⚠️ inplay error:", js)
            return []
    except Exception as e:
        print("❌ inplay request failed:", e)
        return []

def fetch_event_stats(fi_id: str):
    # לפי התמיכה: stats ע"י FI=<EV_ID>
    url = f"{BET_BASE}/event"
    params = {"FI": fi_id, "stats": 1, "token": BETSAPI_TOKEN}
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        # שמים לוג רק אם יש בעיה או לצורכי דיבוג
        if r.status_code != 200:
            print(f"⚠️ event HTTP={r.status_code} url={r.url}")
            return None
        js = r.json()
        if js.get("success") == 1:
            return js.get("results")
        else:
            err = {"success": js.get("success"), "error": js.get("error"), "error_detail": js.get("error_detail")}
            print(f"⚠️ event error for FI={fi_id}: {err}")
            return None
    except Exception as e:
        print(f"❌ event request failed for FI={fi_id}:", e)
        return None

def extract_team_stats(stats_payload):
    """
    Normalize home/away stats. ברירת מחדל:
    S1=On Target, S2=Off Target, SC=Goals, XG אפשרי, Corners מנסים לזהות,
    KP (key passes) אם קיים.
    """
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

    def get_corners(d):
        for k in ("S10", "S11", "CO", "Corners", "corners", "C"):
            if k in d:
                return to_int(d.get(k, 0), 0)
        return 0

    def get_keypasses(d):
        for k in ("KP", "KeyPasses", "key_passes", "keypasses"):
            if k in d:
                return to_int(d.get(k, 0), 0)
        return 0

    def get_yellows(d):
        for k in ("YC", "Y", "Yellow", "yellow_cards", "yellow"):
            if k in d:
                return to_int(d.get(k, 0), 0)
        return 0

    def has_red(d):
        for k in ("RC", "R", "red", "red_cards"):
            if k in d:
                return to_int(d.get(k, 0), 0) >= 1 or str(d.get(k, "0")) == "1"
        return False

    def get_xg(d):
        for k in ("XG", "xg"):
            if k in d and d.get(k) not in ("", None):
                return to_float(d.get(k), 0.0)
        return 0.0

    H = {
        "sot":      to_int(home.get("S1", 0), 0),
        "shots":    to_int(home.get("S1", 0), 0) + to_int(home.get("S2", 0), 0),
        "corners":  get_corners(home),
        "xg":       get_xg(home),
        "kpass":    get_keypasses(home),
        "yellows":  get_yellows(home),
        "has_red":  has_red(home),
        "goals":    to_int(home.get("SC", 0), 0),
        "team_id":  first_nonempty(home.get("TD"), home.get("team_id"), "Home"),
    }
    A = {
        "sot":      to_int(away.get("S1", 0), 0),
        "shots":    to_int(away.get("S1", 0), 0) + to_int(away.get("S2", 0), 0),
        "corners":  get_corners(away),
        "xg":       get_xg(away),
        "kpass":    get_keypasses(away),
        "yellows":  get_yellows(away),
        "has_red":  has_red(away),
        "goals":    to_int(away.get("SC", 0), 0),
        "team_id":  first_nonempty(away.get("TD"), away.get("team_id"), "Away"),
    }
    return H, A

def check_and_alert(ev: dict, minute: int, header_line: str, fi_id: str, H: dict, A: dict):
    alerts = []

    # 6+ SOT ללא גול
    if H["sot"] >= SOT_MIN and H["goals"] == 0:
        alerts.append(("sot6_home", f"🎯 {header_line}\nHome: {H['sot']} shots on target, no goals. ({minute}')"))
    if A["sot"] >= SOT_MIN and A["goals"] == 0:
        alerts.append(("sot6_away", f"🎯 {header_line}\nAway: {A['sot']} shots on target, no goals. ({minute}')"))

    # 12+ בעיטות סה״כ ללא גול
    if H["shots"] >= SHOTS_MIN and H["goals"] == 0:
        alerts.append(("shots12_home", f"🔥 {header_line}\nHome: {H['shots']} total shots, no goals. ({minute}')"))
    if A["shots"] >= SHOTS_MIN and A["goals"] == 0:
        alerts.append(("shots12_away", f"🔥 {header_line}\nAway: {A['shots']} total shots, no goals. ({minute}')"))

    # 8+ קרנות ללא גול
    if H["corners"] >= CORNERS_MIN and H["goals"] == 0:
        alerts.append(("corners8_home", f"🏁 {header_line}\nHome: {H['corners']} corners, no goals. ({minute}')"))
    if A["corners"] >= CORNERS_MIN and A["goals"] == 0:
        alerts.append(("corners8_away", f"🏁 {header_line}\nAway: {A['corners']} corners, no goals. ({minute}')"))

    # xG ≥ 1.0 ללא גול
    if H["xg"] >= XG_MIN and H["goals"] == 0:
        alerts.append(("xg1_home", f"📊 {header_line}\nHome xG={H['xg']:.2f}, no goals. ({minute}')"))
    if A["xg"] >= XG_MIN and A["goals"] == 0:
        alerts.append(("xg1_away", f"📊 {header_line}\nAway xG={A['xg']:.2f}, no goals. ({minute}')"))

    # 6+ key passes ללא גול
    if H["kpass"] >= KPASS_MIN and H["goals"] == 0:
        alerts.append(("kpass6_home", f"🎯 {header_line}\nHome: {H['kpass']} key passes, no goals. ({minute}')"))
    if A["kpass"] >= KPASS_MIN and A["goals"] == 0:
        alerts.append(("kpass6_away", f"🎯 {header_line}\nAway: {A['kpass']} key passes, no goals. ({minute}')"))

    # אדום (כל צד)
    if H["has_red"]:
        alerts.append(("red_home", f"🟥 {header_line}\nRed card for Home. ({minute}')"))
    if A["has_red"]:
        alerts.append(("red_away", f"🟥 {header_line}\nRed card for Away. ({minute}')"))

    # מחצית ללא צהובים
    if minute == 45 and (H["yellows"] + A["yellows"] == 0):
        alerts.append(("ht_no_yellows", f"⚠️ {header_line}\n45': No yellow cards so far."))

    for tag, msg in alerts:
        key = f"{fi_id}:{tag}"
        if key in sent_alerts:
            continue
        send_telegram(msg)
        print("🔔", msg.replace("\n", " | "))
        sent_alerts.add(key)

# ================== MAIN LOOP ==================
def main():
    if not BETSAPI_TOKEN:
        print("🚫 Missing BETSAPI_TOKEN env var")
        return

    print("🚀 Script started successfully — waiting for first scan...")
    print("🔑 BETSAPI token loaded:", BETSAPI_TOKEN[:4] + "…" + BETSAPI_TOKEN[-4:])
    tg_targets = [c for c in (TELEGRAM_CHAT_ID_1, TELEGRAM_CHAT_ID_2) if c]
    print("📬 Telegram targets:", tg_targets)
    send_telegram("✅ Bot started — live monitoring is ON")

    last_hb = time.time()

    while True:
        now = datetime.now(TZ)
        day_mode = (9 <= now.hour < 23) or (now.hour == 23 and now.minute <= 30)
        interval = SCAN_INTERVAL_DAY if day_mode else SCAN_INTERVAL_NGT

        print(f"\n🔄 Scan @ {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | interval={interval}s")

        try:
            res = fetch_inplay()
            evs = flatten_results(res)

            # סינון לאירועי כדורגל רלוונטיים (למרות sport_id=1, מסנן Esoccer/Virtual)
            filtered = []
            for item in evs:
                if not (isinstance(item, dict) and item.get("type") == "EV"):
                    continue
                name   = first_nonempty(item.get("NA"), "")
                comp   = first_nonempty(item.get("CT"), item.get("CN"), "")
                if is_virtual_or_esports(name, comp):
                    continue
                filtered.append(item)

            print(f"📊 INPLAY rows: {len(evs)} | filtered (soccer-only, no virtual/esoccer): {len(filtered)}")

            if not filtered:
                print("ℹ️ No matches in play.")
            for ev in filtered:
                fi_id   = ev.get("ID")
                if not fi_id:
                    continue

                home    = first_nonempty(ev.get("HOME"), ev.get("home"), ev.get("T1"))
                away    = first_nonempty(ev.get("AWAY"), ev.get("away"), ev.get("T2"))
                score   = first_nonempty(ev.get("SS"), ev.get("ss")) or "?-?"
                minute  = parse_minute(first_nonempty(ev.get("TM"), ev.get("time"), ev.get("timer")))
                country = first_nonempty(ev.get("CB"), ev.get("CN"))  # CB לפעמים קוד מדינה; CN/CT ליגה/מדינה
                league  = first_nonempty(ev.get("CT"), ev.get("CL"))

                if minute > MAX_MINUTE:
                    continue

                header = match_header(country, league, home, away)
                # לוג קצר לכל משחק לפני משיכה מפורטת
                print(f"   · {country or '—'} — {league or '—'}, {minute}' | {home or 'Home'} vs {away or 'Away'} | FI={fi_id}")

                details = fetch_event_stats(fi_id)
                if not details:
                    continue

                # details יכול להיות list או dict:
                stats_payload = None
                if isinstance(details, list):
                    if len(details) > 0 and isinstance(details[0], dict) and "stats" in details[0]:
                        stats_payload = details[0]["stats"]
                    else:
                        stats_payload = details
                elif isinstance(details, dict):
                    stats_payload = details.get("stats") or details

                H, A = extract_team_stats(stats_payload)
                if H is None or A is None:
                    continue

                check_and_alert(ev, minute, header, fi_id, H, A)

        except Exception as e:
            print("❌ Scan exception:", e)

        # Heartbeat כל 10 שעות
        if time.time() - last_hb >= HEARTBEAT_SEC:
            send_telegram("✅ Heartbeat — bot is running")
            last_hb = time.time()

        time.sleep(interval)

if __name__ == "__main__":
    main()
