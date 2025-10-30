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

# סריקות: 09:00–23:30 כל 30 שניות; 23:31–09:00 כל 8 דקות
SCAN_INTERVAL_DAY  = 30
SCAN_INTERVAL_NGT  = 480

MAX_MINUTE         = 65          # לא סורקים/מתריעים אחרי דקה 65
HEARTBEAT_SEC      = 10 * 60 * 60  # כל 10 שעות

# ספי התראות
SOT_MIN            = 6
SHOTS_MIN          = 12
CORNERS_MIN        = 8
XG_MIN             = 1.0
KPASS_MIN          = 6

# למניעת כפילויות
sent_alerts = set()

# ================== TELEGRAM ==================
def send_telegram(text: str):
    targets = []
    if TELEGRAM_TOKEN_1 and TELEGRAM_CHAT_ID_1:
        targets.append((TELEGRAM_TOKEN_1, TELEGRAM_CHAT_ID_1))
    if TELEGRAM_TOKEN_2 and TELEGRAM_CHAT_ID_2:
        targets.append((TELEGRAM_TOKEN_2, TELEGRAM_CHAT_ID_2))

    if not targets:
        print("⚠️ No Telegram targets configured")
        return

    for token, chat_id in targets:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=10
            )
            if r.status_code != 200:
                print(f"⚠️ Telegram {chat_id} error {r.status_code}: {r.text[:180]}")
            else:
                print(f"✅ Telegram sent to {chat_id}")
        except Exception as e:
            print("⚠️ Telegram exception:", e)

# ================== HELPERS ==================
def parse_minute(val) -> int:
    """מוציא דקה מתוך '45', '45+2', int, או None."""
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    m = re.match(r"^\s*(\d+)", str(val))
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
    """results מ-/bet365/inplay מגיעים בד״כ כמקטעים מקוננים."""
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

def fetch_inplay():
    url = f"{BET_BASE}/inplay"
    params = {"sport_id": 1, "token": BETSAPI_TOKEN}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    try:
        js = r.json()
    except Exception:
        print(f"❌ inplay non-JSON (status={r.status_code})")
        return []
    if js.get("success") == 1:
        return js.get("results", [])
    print("⚠️ inplay error:", js)
    return []

def fetch_event_stats(fi_id: str):
    """על פי התמיכה: stats נגיש דרך FI=<EV_ID>"""
    url = f"{BET_BASE}/event"
    params = {"FI": fi_id, "stats": 1, "token": BETSAPI_TOKEN}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    try:
        js = r.json()
    except Exception:
        print(f"❌ event non-JSON for FI={fi_id} (status={r.status_code})")
        return None
    if js.get("success") == 1:
        return js.get("results")
    # רק לוג, לא עוצרים את הלופ
    print(f"⚠️ event error for FI={fi_id}:", js)
    return None

def extract_team_stats(stats_payload):
    """
    מנרמל מבנה סטטיסטיקות: בד״כ [home, away] עם מפתחות כמו S1,S2,..., XG, SC וכו׳.
    מחזיר שני dict-ים: H ו-A (או (None,None) אם נכשל).
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
        # לרוב S10 מייצג קרנות, אבל נשאיר fallback-ים אם ישתנה בעתיד
        for k in ("S10", "CO", "Corners", "corners"):
            if k in d and str(d.get(k)) != "":
                return to_int(d.get(k), 0)
        return 0

    def get_keypasses(d):
        for k in ("KeyPasses", "key_passes", "KP"):
            if k in d and str(d.get(k)) != "":
                return to_int(d.get(k), 0)
        return 0

    def get_yellows(d):
        for k in ("Y", "yellow", "YC"):
            if k in d and str(d.get(k)) != "":
                return to_int(d.get(k), 0)
        return 0

    def has_red(d):
        for k in ("R", "red", "RC"):
            if k in d and str(d.get(k)) != "":
                return to_int(d.get(k), 0) >= 1
        return False

    def get_xg(d):
        for k in ("XG", "xg"):
            if k in d and d.get(k) not in ("", None):
                return to_float(d.get(k), 0.0)
        return 0.0

    H = {
        "sot":      to_int(home.get("S1", 0)),                         # On Target
        "shots":    to_int(home.get("S1", 0)) + to_int(home.get("S2", 0)),  # On+Off
        "corners":  get_corners(home),
        "xg":       get_xg(home),
        "kpass":    get_keypasses(home),
        "yellows":  get_yellows(home),
        "has_red":  has_red(home),
        "goals":    to_int(home.get("SC", 0)),
    }
    A = {
        "sot":      to_int(away.get("S1", 0)),
        "shots":    to_int(away.get("S1", 0)) + to_int(away.get("S2", 0)),
        "corners":  get_corners(away),
        "xg":       get_xg(away),
        "kpass":    get_keypasses(away),
        "yellows":  get_yellows(away),
        "has_red":  has_red(away),
        "goals":    to_int(away.get("SC", 0)),
    }
    return H, A

def check_and_alert(ev: dict, minute: int, header_line: str, fi_id: str, H: dict, A: dict):
    """שולח התראות לפי החוקים, עם מניעת כפילויות."""
    alerts = []

    # 6+ למסגרת ללא גול
    if H["sot"] >= SOT_MIN and H["goals"] == 0:
        alerts.append(("sot6_home", f"🎯 {header_line}\nHome: {H['sot']} shots on target, 0 goals."))
    if A["sot"] >= SOT_MIN and A["goals"] == 0:
        alerts.append(("sot6_away", f"🎯 {header_line}\nAway: {A['sot']} shots on target, 0 goals."))

    # 12+ בעיטות סה״כ ללא גול
    if H["shots"] >= SHOTS_MIN and H["goals"] == 0:
        alerts.append(("shots12_home", f"🔥 {header_line}\nHome: {H['shots']} total shots, 0 goals."))
    if A["shots"] >= SHOTS_MIN and A["goals"] == 0:
        alerts.append(("shots12_away", f"🔥 {header_line}\nAway: {A['shots']} total shots, 0 goals."))

    # 8+ קרנות ללא גול
    if H["corners"] >= CORNERS_MIN and H["goals"] == 0:
        alerts.append(("corners8_home", f"🏁 {header_line}\nHome: {H['corners']} corners, 0 goals."))
    if A["corners"] >= CORNERS_MIN and A["goals"] == 0:
        alerts.append(("corners8_away", f"🏁 {header_line}\nAway: {A['corners']} corners, 0 goals."))

    # xG ≥ 1.0 ללא גול
    if H["xg"] >= XG_MIN and H["goals"] == 0:
        alerts.append(("xg1_home", f"📊 {header_line}\nHome xG={H['xg']:.2f}, 0 goals."))
    if A["xg"] >= XG_MIN and A["goals"] == 0:
        alerts.append(("xg1_away", f"📊 {header_line}\nAway xG={A['xg']:.2f}, 0 goals."))

    # 6+ key passes ללא גול
    if H["kpass"] >= KPASS_MIN and H["goals"] == 0:
        alerts.append(("kpass6_home", f"🎯 {header_line}\nHome: {H['kpass']} key passes, 0 goals."))
    if A["kpass"] >= KPASS_MIN and A["goals"] == 0:
        alerts.append(("kpass6_away", f"🎯 {header_line}\nAway: {A['kpass']} key passes, 0 goals."))

    # כרטיס אדום (כל צד)
    if H["has_red"]:
        alerts.append(("red_home", f"🟥 {header_line}\nRed card for Home."))
    if A["has_red"]:
        alerts.append(("red_away", f"🟥 {header_line}\nRed card for Away."))

    # מחצית ללא צהובים
    if minute == 45 and (H["yellows"] + A["yellows"] == 0):
        alerts.append(("ht_no_yellows", f"⚠️ {header_line}\n45': No yellow cards so far."))

    # שליחה עם דה-דופליקציה
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
    tg_targets = [t for t in [TELEGRAM_CHAT_ID_1, TELEGRAM_CHAT_ID_2] if t]
    print("📬 Telegram targets:", tg_targets)
    send_telegram("✅ Bot started — monitoring live matches.")

    last_hb = time.time()

    while True:
        now = datetime.now(TZ)
        day_mode = (9 <= now.hour < 23) or (now.hour == 23 and now.minute <= 30)
        interval = SCAN_INTERVAL_DAY if day_mode else SCAN_INTERVAL_NGT

        print(f"\n🔄 Scan @ {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | interval={interval}s")

        try:
            res = fetch_inplay()
            evs = flatten_results(res)
            print(f"📊 INPLAY rows (raw): {len(evs)}")

            if len(evs) == 0:
                print("ℹ️ No in-play data from API at this moment.")

            eligible = 0
            processed = 0

            for ev in evs:
                if not isinstance(ev, dict) or ev.get("type") != "EV":
                    continue

                processed += 1

                fi_id = ev.get("ID") or ev.get("FI")
                if not fi_id:
                    continue

                # שמות/פרטים
                home = first_nonempty(ev.get("HOME"), ev.get("home"), ev.get("T1"), "Home")
                away = first_nonempty(ev.get("AWAY"), ev.get("away"), ev.get("T2"), "Away")
                country = first_nonempty(ev.get("CN"), ev.get("cc"))
                league  = first_nonempty(ev.get("CT"), ev.get("league"), ev.get("LG"))

                minute = parse_minute(first_nonempty(ev.get("TM"), ev.get("time"), ev.get("timer")))
                if minute > MAX_MINUTE:
                    continue

                eligible += 1
                print(f"   · {country or '—'} — {league or '—'}, {minute}' | {home} vs {away} | FI={fi_id}")

                details = fetch_event_stats(fi_id)
                if not details:
                    continue

                # details יכול להיות list או dict
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

                header = match_header(country, league, home, away)
                check_and_alert(ev, minute, header, fi_id, H, A)

            print(f"🧾 Processed EV nodes: {processed} | Eligible (≤{MAX_MINUTE}'): {eligible}")
            if processed > 0 and eligible == 0:
                print("ℹ️ There are in-play matches, but none eligible (minute > limit or missing stats).")

        except Exception as e:
            print("❌ Scan exception:", e)

        # Heartbeat
        if time.time() - last_hb >= HEARTBEAT_SEC:
            send_telegram("💓 Heartbeat — bot is running")
            last_hb = time.time()

        time.sleep(interval)

if __name__ == "__main__":
    main()
