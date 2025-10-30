import os, re, time, requests
from datetime import datetime
import pytz

# ================== ENV VARS ==================
BETSAPI_TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()

TELEGRAM_TOKEN_1   = os.getenv("TELEGRAM_TOKEN_1", "").strip()
TELEGRAM_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1", "").strip()
TELEGRAM_TOKEN_2   = os.getenv("TELEGRAM_TOKEN_2", "").strip()
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()

# ================== CONFIG ==================
TZ = pytz.timezone("Asia/Jerusalem")
BET_BASE = "https://api.betsapi.com/v1/bet365"
TIMEOUT = 20

SCAN_INTERVAL_DAY = 30     # 09:00–23:30
SCAN_INTERVAL_NGT = 480    # 23:31–08:59
MAX_MINUTE = 65
HEARTBEAT_SEC = 10 * 60 * 60  # 10 hours

# Alert thresholds
SOT_MIN = 6
SHOTS_MIN = 12
CORNERS_MIN = 8
XG_MIN = 1.0
KPASS_MIN = 6

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
                print(f"⚠️ Telegram {chat_id} error {r.status_code}: {r.text[:100]}")
            else:
                print(f"✅ Telegram sent to {chat_id}")
        except Exception as e:
            print("⚠️ Telegram exception:", e)

# ================== HELPERS ==================
def parse_minute(v): 
    if not v: return 0
    m = re.match(r"(\d+)", str(v))
    return int(m.group(1)) if m else 0

def to_int(x, d=0):
    try: return int(float(x))
    except: return d

def to_float(x, d=0.0):
    try: return float(x)
    except: return d

def first_nonempty(*vals):
    for v in vals:
        if v not in (None, "", "None"): return v
    return None

def match_header(country, league, home, away):
    cn = country or "—"; lg = league or "—"
    return f"{cn} — {lg}\n{home or 'Home'} vs {away or 'Away'}"

# ================== FETCH ==================
def fetch_inplay():
    url = f"{BET_BASE}/inplay"
    r = requests.get(url, params={"sport_id": 1, "token": BETSAPI_TOKEN}, timeout=TIMEOUT)
    js = r.json()
    return js.get("results", []) if js.get("success") == 1 else []

def fetch_event_stats(fi_id: str):
    for mode in ("FI", "id"):
        try:
            url = f"{BET_BASE}/event"
            r = requests.get(url, params={mode: fi_id, "stats": 1, "token": BETSAPI_TOKEN}, timeout=TIMEOUT)
            js = r.json()
            if js.get("success") == 1:
                return js.get("results")
        except Exception:
            pass
    return None

# ================== STATS PARSE ==================
def extract_team_stats(payload):
    if isinstance(payload, list) and len(payload) >= 2:
        home, away = payload[0], payload[1]
    elif isinstance(payload, dict) and "stats" in payload:
        s = payload["stats"]
        if isinstance(s, list) and len(s) >= 2: home, away = s[0], s[1]
        else: return None, None
    else: return None, None

    def get_xg(d): return to_float(d.get("XG", 0.0))
    H = {
        "sot": to_int(d := home.get("S1", 0)),
        "shots": to_int(home.get("S1", 0)) + to_int(home.get("S2", 0)),
        "corners": to_int(home.get("S11", 0)),
        "xg": get_xg(home),
        "kpass": to_int(home.get("KP", 0)),
        "yellows": to_int(home.get("YC", 0)),
        "has_red": str(home.get("RC", "0")) == "1",
        "goals": to_int(home.get("SC", 0)),
    }
    A = {
        "sot": to_int(away.get("S1", 0)),
        "shots": to_int(away.get("S1", 0)) + to_int(away.get("S2", 0)),
        "corners": to_int(away.get("S11", 0)),
        "xg": get_xg(away),
        "kpass": to_int(away.get("KP", 0)),
        "yellows": to_int(away.get("YC", 0)),
        "has_red": str(away.get("RC", "0")) == "1",
        "goals": to_int(away.get("SC", 0)),
    }
    return H, A

# ================== ALERTS ==================
def check_and_alert(ev, minute, header, fi_id, H, A):
    alerts = []
    if H["sot"] >= SOT_MIN and H["goals"] == 0:
        alerts.append(("sot6_home", f"🎯 {header}\nHome: {H['sot']} SOT, no goals"))
    if A["sot"] >= SOT_MIN and A["goals"] == 0:
        alerts.append(("sot6_away", f"🎯 {header}\nAway: {A['sot']} SOT, no goals"))
    if H["shots"] >= SHOTS_MIN and H["goals"] == 0:
        alerts.append(("shots12_home", f"🔥 {header}\nHome: {H['shots']} shots, no goals"))
    if A["shots"] >= SHOTS_MIN and A["goals"] == 0:
        alerts.append(("shots12_away", f"🔥 {header}\nAway: {A['shots']} shots, no goals"))
    if H["corners"] >= CORNERS_MIN and H["goals"] == 0:
        alerts.append(("corners8_home", f"🏁 {header}\nHome: {H['corners']} corners, no goals"))
    if A["corners"] >= CORNERS_MIN and A["goals"] == 0:
        alerts.append(("corners8_away", f"🏁 {header}\nAway: {A['corners']} corners, no goals"))
    if H["xg"] >= XG_MIN and H["goals"] == 0:
        alerts.append(("xg1_home", f"📊 {header}\nHome xG={H['xg']:.2f}, no goals"))
    if A["xg"] >= XG_MIN and A["goals"] == 0:
        alerts.append(("xg1_away", f"📊 {header}\nAway xG={A['xg']:.2f}, no goals"))
    if H["kpass"] >= KPASS_MIN and H["goals"] == 0:
        alerts.append(("kp_home", f"🎯 {header}\nHome: {H['kpass']} key passes, no goals"))
    if A["kpass"] >= KPASS_MIN and A["goals"] == 0:
        alerts.append(("kp_away", f"🎯 {header}\nAway: {A['kpass']} key passes, no goals"))
    if H["has_red"] or A["has_red"]:
        alerts.append(("red_card", f"🟥 {header}\nRed card issued"))
    if minute == 45 and H["yellows"] + A["yellows"] == 0:
        alerts.append(("no_yellow_ht", f"⚠️ {header}\n45': No yellow cards"))

    for tag, msg in alerts:
        key = f"{fi_id}:{tag}"
        if key not in sent_alerts:
            send_telegram(msg)
            print("🔔", msg.replace("\n", " | "))
            sent_alerts.add(key)

# ================== MAIN LOOP ==================
def main():
    if not BETSAPI_TOKEN:
        print("🚫 Missing BETSAPI_TOKEN"); return

    print(f"🔑 BETSAPI token loaded: {BETSAPI_TOKEN[:4]}…{BETSAPI_TOKEN[-4:]}")
    print(f"📬 Telegram targets:", [t for t in (TELEGRAM_CHAT_ID_1, TELEGRAM_CHAT_ID_2) if t])
    send_telegram("✅ Bot started — monitoring live matches.")
    last_hb = time.time()

    while True:
        now = datetime.now(TZ)
        day_mode = (9 <= now.hour < 23) or (now.hour == 23 and now.minute <= 30)
        interval = SCAN_INTERVAL_DAY if day_mode else SCAN_INTERVAL_NGT
        print(f"🔄 Scan @ {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | interval={interval}s")

        try:
            inplay = fetch_inplay()
            print(f"📊 INPLAY rows: {len(inplay)}")
            for ev in inplay:
                if ev.get("type") != "EV": continue
                fi_id = ev.get("ID");  minute = parse_minute(first_nonempty(ev.get("TM"), ev.get("time")))
                if not fi_id or minute > MAX_MINUTE: continue

                country = first_nonempty(ev.get("CN"), ev.get("cc"))
                league  = first_nonempty(ev.get("CL"), ev.get("league"))
                home = first_nonempty(ev.get("HOME"), ev.get("T1"))
                away = first_nonempty(ev.get("AWAY"), ev.get("T2"))
                header = match_header(country, league, home, away)

                details = fetch_event_stats(fi_id)
                if not details: continue

                stats_payload = details[0].get("stats") if isinstance(details, list) and details and isinstance(details[0], dict) else details
                H, A = extract_team_stats(stats_payload)
                if H and A: check_and_alert(ev, minute, header, fi_id, H, A)

        except Exception as e:
            print("❌ Exception:", e)

        if time.time() - last_hb >= HEARTBEAT_SEC:
            send_telegram("💓 Heartbeat — bot active.")
            last_hb = time.time()

        time.sleep(interval)

if __name__ == "__main__":
    print("🚀 Script started successfully — waiting for first scan...")
    main()
