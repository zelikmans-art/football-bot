import os, sys, time, json
from datetime import datetime, timezone
import requests

# ===== Config =====
SCAN_INTERVAL_SEC = 120
TIMEOUT_SEC = 20

# ===== ENV =====
TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not TOKEN:
    print("🚫 Missing BETSAPI_TOKEN env var"); sys.exit(1)
if not TG_TOKEN or not TG_CHAT:
    print("⚠️ Telegram env vars missing; will print only to logs")

session = requests.Session()

def tgsend(msg: str):
    if not TG_TOKEN or not TG_CHAT: 
        return
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=10
        )
        if r.status_code != 200:
            print(f"⚠️ Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print("⚠️ Telegram exception:", e)

def get_json(url: str, kind: str, params=None):
    try:
        r = session.get(url, params=params, timeout=TIMEOUT_SEC)
        print(f"🌐 {kind} {r.status_code} → {r.url}")
    except Exception as e:
        print(f"❌ {kind} network error: {e}")
        return None
    ct = r.headers.get("content-type","")
    if "application/json" not in ct:
        print(f"❌ {kind} non-JSON: {r.text[:300]}")
        return None
    try:
        js = r.json()
    except Exception:
        print(f"❌ {kind} JSON parse error: {r.text[:300]}")
        return None
    # BetsAPI נוהג להחזיר success/results/error
    if isinstance(js, dict):
        print("ℹ️", kind, "success:", js.get("success"), "| results:", js.get("results"), "| error:", js.get("error") or js.get("errors"))
    return js

# ---- BetsAPI endpoints ----
def fetch_inplay_soccer():
    # sport_id=1 -> Soccer
    url = "https://api.b365api.com/v3/events/inplay"
    return get_json(url, "inplay", {"sport_id": 1, "token": TOKEN})

def fetch_event_view(event_id):
    url = "https://api.b365api.com/v1/event/view"
    return get_json(url, f"event_view:{event_id}", {"event_id": event_id, "token": TOKEN})

def fetch_stats_trend(event_id):
    # לא תמיד קיים לכל משחק/חבילה, אבל ננסה ונדפיס דוגמית אם יש
    url = "https://api.b365api.com/v1/event/stats_trend"
    return get_json(url, f"stats_trend:{event_id}", {"event_id": event_id, "token": TOKEN})

# ---- helpers to extract stats from event_view payloads (שונות בין ליגות/פיצ'רים) ----
def try_extract_basic_stats(ev_view):
    """
    מחפש שדות כמו shots_on_target / shots / corners אם קיימים.
    בגלל שהפורמט משתנה, נעבור בצורה חסינה על כל dict/array ונאתר מפתחות שמכילים את המילים הרלוונטיות.
    """
    out = {"SOT": None, "Shots": None, "Corners": None}
    if not isinstance(ev_view, dict):
        return out

    def scan_obj(obj):
        # obj יכול להיות dict או list; נסרוק רק מפתחות/מחרוזות
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if any(t in kl for t in ["shot_on_target","shots_on_target","shots on target","sot"]):
                    out["SOT"] = safe_num(v)
                if "corner" in kl:
                    out["Corners"] = max_non_none(out["Corners"], safe_num(v))
                if ("total_shots" in kl) or ("shots_total" in kl) or (kl == "shots") or ("shots" in kl and "on" not in kl):
                    out["Shots"] = max_non_none(out["Shots"], safe_num(v))
                # רקורסיה
                scan_obj(v)
        elif isinstance(obj, list):
            for it in obj:
                scan_obj(it)

    scan_obj(ev_view)
    return out

def max_non_none(a, b):
    if a is None: return b
    if b is None: return a
    try:
        return max(float(a), float(b))
    except:
        return a

def safe_num(v):
    # מנסה להמיר ערכים כמו "7", "7:3", "7-3", "7%" וכו'
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip()
    # pair "H:A"
    if ":" in s:
        try:
            h, a = s.split(":", 1)
            return float(h), float(a)
        except:
            return None
    # remove % or other junk
    s = s.rstrip("%")
    try:
        return float(s)
    except:
        return None

def count_red_cards_from_events(ev_view):
    reds_home = reds_away = 0
    # מחפש מערך events אם קיים
    events = None
    # מיקומים אפשריים
    for key in ["events", "event", "timeline"]:
        if isinstance(ev_view, dict) and key in ev_view:
            events = ev_view[key]; break
    if not isinstance(events, list):
        return 0, 0
    for e in events:
        # בדוק יש "type/detail" שמכילים red
        text = " ".join(str(e.get(k,"")).lower() for k in ["type","detail","desc","comment"])
        if "red" in text:
            # נסה להבין בית/חוץ
            side = (e.get("side") or e.get("team") or "").lower()
            if side in ("home","h","1"):
                reds_home += 1
            elif side in ("away","a","2"):
                reds_away += 1
            else:
                # fallback: אם יש player_home/player_away
                if e.get("player_home") and not e.get("player_away"): reds_home += 1
                elif e.get("player_away") and not e.get("player_home"): reds_away += 1
                else:
                    # לא ברור—לא נספור
                    pass
    return reds_home, reds_away

# ---- main scan ----
def scan_once():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"🔄 Scan @ {ts}")

    js_inplay = fetch_inplay_soccer()
    if not isinstance(js_inplay, dict):
        print("❌ Bad inplay response"); return

    # חלק מהתגובות מגיעות תחת keys שונים. בד"כ data או results/ pager… ננסה כמה אופציות
    events = js_inplay.get("results") or js_inplay.get("data") or js_inplay.get("events") or []
    if isinstance(events, dict):
        # לפעמים events באובייקט עם מפתח data
        events = events.get("data") or []

    print(f"📊 Inplay count: {len(events)}")

    # אם אין אירועים—שגר התראה חד־פעמית כדי לדעת שהמפתח עובד אבל אין נתונים
    if not events:
        print("ℹ️ No inplay events right now.")
        return

    # עבור כל משחק הצג פרטים בסיסיים, ושלוף event/view כדי לבדוק אם יש סטטיסטיקות
    for idx, ev in enumerate(events[:20], start=1):  # הגבלת דוגמא לוגים ל-20 כדי לא להציף
        event_id = ev.get("id") or ev.get("event_id") or ev.get("FI") or ev.get("EV")  # שמות נפוצים
        home = ev.get("home", ev.get("home_team"))
        away = ev.get("away", ev.get("away_team"))
        league = ev.get("league", ev.get("league_name") or ev.get("league_str"))
        cc = ev.get("cc", ev.get("country", ev.get("country_name")))
        minute = ev.get("timer", ev.get("time", ev.get("match_time")))
        score = ev.get("ss") or f"{ev.get('home_score','?')}-{ev.get('away_score','?')}"

        print(f"   • [{idx}] {cc or '—'} / {league or '—'} | {minute or 'N/A'} | {home} {score} {away} | id={event_id}")

        if not event_id:
            continue

        view = fetch_event_view(event_id)
        if not isinstance(view, dict):
            print("      ↪ no event_view json")
            continue

        # לרוב ה-payload האמיתי נמצא תחת 'results' או 'data'
        payload = view.get("results") or view.get("data") or view
        # הדפס sample keys פעם ראשונה כדי להבין מה יש
        if idx <= 3:
            # הצג רק כותרות ו-sample קטן
            print("      ↪ keys:", list(payload.keys())[:10] if isinstance(payload, dict) else type(payload))

        # נסה לחלץ סטטיסטיקות
        stats = try_extract_basic_stats(payload)
        reds_h, reds_a = count_red_cards_from_events(payload)

        # הדפס תקציר לכל משחק
        print(f"      ↪ Stats? SOT={stats['SOT']} | Shots={stats['Shots']} | Corners={stats['Corners']} | Reds H/A={reds_h}/{reds_a}")

        # אופציונלי: ננסה גם stats_trend (לא לכל אחד יש)
        if idx <= 2:
            st = fetch_stats_trend(event_id)
            if isinstance(st, dict):
                # הדפס כמה שדות לדוגמה כדי שתראה אם זה פעיל אצלך
                sample = st.get("results") or st.get("data") or {}
                print("      ↪ stats_trend sample keys:", list(sample.keys())[:10] if isinstance(sample, dict) else type(sample))

if __name__ == "__main__":
    print("🔑 Using BetsAPI token:", TOKEN[:4] + "…" + TOKEN[-4:] if len(TOKEN) >= 8 else TOKEN)
    if TG_TOKEN and TG_CHAT:
        tgsend("✅ BetsAPI test bot started (single-provider).")

    while True:
        try:
            scan_once()
        except Exception as e:
            print("❌ Uncaught:", e)
            tgsend(f"❌ Uncaught: {e}")
            time.sleep(5)
        time.sleep(SCAN_INTERVAL_SEC)
