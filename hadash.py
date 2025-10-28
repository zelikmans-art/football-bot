import os, sys, time, json
from datetime import datetime, timezone
import requests

# ============== CONFIG ==============
SCAN_INTERVAL_SEC      = 120     # כל 2 דקות
TIMEOUT_SEC            = 20
HEARTBEAT_EVERY_SEC    = 10 * 60 * 60  # כל 10 שעות
WATCHDOG_STALL_SEC     = 15 * 60       # אין התקדמות 15 דק' → הודעת אזהרה

# Thresholds (עם מגבלת דקה ≤ 60)
XG_THRESHOLD           = 0.8
SOT_THRESHOLD          = 4
CORNERS_THRESHOLD      = 6
TOTAL_SHOTS_THRESHOLD  = 8
MAX_ALERT_MINUTE       = 60

# ============== TOKENS (BetsAPI) ==============
# לפי בקשתך: הטוקן מוכנס ישירות לקוד. מומלץ בעתיד לעבור ל-ENV.
BETSAPI_TOKEN = "236044-vjHdM29EvfZhfx"

# ============== TELEGRAM DESTINATIONS ==============
def get_env(name, default=""):
    return os.getenv(name, default).strip()

TELEGRAM_TOKEN     = get_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = get_env("TELEGRAM_CHAT_ID")
TELEGRAM_TOKEN_2   = get_env("TELEGRAM_TOKEN_2")
TELEGRAM_CHAT_ID_2 = get_env("TELEGRAM_CHAT_ID_2")
TELEGRAM_DESTS_CSV = get_env("TELEGRAM_DESTINATIONS")  # "TOKEN|CHATID,TOKEN|CHATID,..."

def build_destinations():
    dests = []
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        dests.append((TELEGRAM_TOKEN, TELEGRAM_CHAT_ID))
    if TELEGRAM_TOKEN_2 and TELEGRAM_CHAT_ID_2:
        dests.append((TELEGRAM_TOKEN_2, TELEGRAM_CHAT_ID_2))
    if TELEGRAM_DESTS_CSV:
        for part in TELEGRAM_DESTS_CSV.split(","):
            part = part.strip()
            if "|" in part:
                tok, cid = part.split("|", 1)
                if tok.strip() and cid.strip():
                    dests.append((tok.strip(), cid.strip()))
    # dedupe
    seen, uniq = set(), []
    for t, c in dests:
        k = f"{t}:{c}"
        if k not in seen:
            uniq.append((t, c)); seen.add(k)
    return uniq

DESTINATIONS = build_destinations()
if not DESTINATIONS:
    print("🚫 No Telegram destinations set. Please set TELEGRAM_TOKEN & TELEGRAM_CHAT_ID.", flush=True)

# ============== STATE ==============
start_time        = datetime.now(timezone.utc)
last_heartbeat_ts = 0.0
last_progress_ts  = time.time()
scan_count        = 0
last_scan_time    = None
sent_alerts       = set()

session = requests.Session()

# ============== TELEGRAM ==============
def tgsend_all(text: str):
    if not DESTINATIONS:
        return
    for token, chat_id in DESTINATIONS:
        try:
            r = session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=TIMEOUT_SEC
            )
            if r.status_code != 200:
                print(f"⚠️ Telegram {chat_id} {r.status_code}: {r.text[:250]}", flush=True)
            else:
                print(f"✅ Telegram sent to {chat_id}.", flush=True)
        except Exception as e:
            print(f"❌ Telegram exception to {chat_id}: {e}", flush=True)

def maybe_heartbeat(force=False):
    global last_heartbeat_ts
    now = time.time()
    if force or (now - last_heartbeat_ts >= HEARTBEAT_EVERY_SEC):
        uptime = str((datetime.now(timezone.utc) - start_time)).split(".")[0]
        last   = last_scan_time.strftime("%Y-%m-%d %H:%M:%S UTC") if last_scan_time else "N/A"
        dests  = ", ".join([cid for _, cid in DESTINATIONS]) or "none"
        tgsend_all(f"✅ Heartbeat\nUptime: {uptime}\nScans: {scan_count}\nLast scan: {last}\nDestinations: {dests}\nProvider: BetsAPI")
        last_heartbeat_ts = now

def watchdog_ping():
    global last_progress_ts
    if time.time() - last_progress_ts >= WATCHDOG_STALL_SEC:
        tgsend_all("⚠️ Watchdog: no scan progress in the last 15 minutes. Continuing…")
        last_progress_ts = time.time()

# ============== BETSAPI HELPERS ==============
def api_get(url, kind, params=None):
    params = dict(params or {})
    params["token"] = BETSAPI_TOKEN
    try:
        r = session.get(url, params=params, timeout=TIMEOUT_SEC)
    except Exception as e:
        print(f"❌ {kind} network error: {e}", flush=True)
        return None
    print(f"🌐 {kind} HTTP={r.status_code} → {r.url}", flush=True)
    ct = r.headers.get("content-type","")
    if "application/json" not in ct:
        print(f"❌ {kind} non-JSON: {r.text[:300]}", flush=True)
        return None
    try:
        js = r.json()
    except Exception:
        print(f"❌ {kind} JSON parse error: {r.text[:300]}", flush=True)
        return None
    if isinstance(js, dict):
        print("ℹ️", kind, "success:", js.get("success"), "| results:", js.get("results") if isinstance(js.get("results"), list) else type(js.get("results")), "| error:", js.get("error") or js.get("errors"), "| detail:", js.get("error_detail"), flush=True)
    return js

def fetch_inplay():
    # Soccer sport_id=1
    return api_get("https://api.b365api.com/v3/events/inplay", "inplay", {"sport_id": 1})

def fetch_event_view(event_id):
    return api_get("https://api.b365api.com/v1/event/view", f"event_view:{event_id}", {"event_id": event_id})

# --- parsing helpers (חסינים לשינויים) ---
def to_int_minute(minute):
    try:
        if isinstance(minute, (int, float)): return int(minute)
        if isinstance(minute, str): return int(minute.strip().replace("'", ""))
    except:
        return None
    return None

def safe_num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip()
    if ":" in s:  # "7:3" ⇒ נחזיר זוג
        try:
            h, a = s.split(":", 1)
            return float(h), float(a)
        except:
            return None
    s = s.rstrip("%")
    try:
        return float(s)
    except:
        return None

def max_non_none(a, b):
    if a is None: return b
    if b is None: return a
    try:
        return max(float(a), float(b))
    except:
        return a

def try_extract_basic_stats(ev_view, team_side=None):
    """
    מחפש סטטיסטיקות בסיסיות במבנה לא אחיד.
    נחזיר dict: {"SOT": x, "Shots": y, "Corners": z, "xG": w (אם קיים)}
    team_side: "home" / "away" (אם נרצה לנחש לפי שדות צד)
    """
    out = {"SOT": None, "Shots": None, "Corners": None, "xG": None}

    def scan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                # xG
                if "xg" in kl or "expected_goals" in kl or "expected goals" in kl:
                    out["xG"] = max_non_none(out["xG"], safe_num(v))
                # shots on target
                if any(s in kl for s in ["shot_on_target", "shots_on_target", "shots on target", "sot"]):
                    out["SOT"] = max_non_none(out["SOT"], safe_num(v))
                # total shots
                if ("total_shots" in kl) or ("shots_total" in kl) or (kl == "shots") or ("shots" in kl and "on" not in kl):
                    out["Shots"] = max_non_none(out["Shots"], safe_num(v))
                # corners
                if "corner" in kl:
                    out["Corners"] = max_non_none(out["Corners"], safe_num(v))
                scan(v)
        elif isinstance(obj, list):
            for it in obj:
                scan(it)

    if isinstance(ev_view, dict):
        scan(ev_view)
    return out

def count_red_cards_from_events(ev_view, team_hint=None):
    reds_home = reds_away = 0
    events = None
    for key in ["events", "event", "timeline"]:
        if isinstance(ev_view, dict) and key in ev_view:
            events = ev_view[key]; break
    if not isinstance(events, list):
        return reds_home, reds_away
    for e in events:
        text = " ".join(str(e.get(k,"")).lower() for k in ["type","detail","desc","comment"])
        if "red" in text:
            side = (e.get("side") or e.get("team") or "").lower()
            if side in ("home","h","1"): reds_home += 1
            elif side in ("away","a","2"): reds_away += 1
    return reds_home, reds_away

def make_key(event_id, team, rule):
    return f"{event_id}:{team}:{rule}"

# ============== SCAN ==============
def scan_once():
    global scan_count, last_scan_time, last_progress_ts
    last_scan_time = datetime.now(timezone.utc)
    scan_count += 1
    last_progress_ts = time.time()

    print(f"\n🔄 Scan @ {last_scan_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    js = fetch_inplay()
    if not isinstance(js, dict):
        print("❌ inplay: bad json", flush=True); return

    if js.get("success") != 1:
        err = js.get("error")
        if err == "AUTHORIZE_FAILED":
            print("🚫 AUTHORIZE_FAILED → הטוקן עדיין לא פעיל. בדוק https://betsapi.com/mm/orders", flush=True)
        else:
            print(f"⚠️ inplay error: {err} | detail: {js.get('error_detail')}", flush=True)
        return

    events = js.get("results") or js.get("data") or js.get("events") or []
    if isinstance(events, dict):
        events = events.get("data") or []
    print(f"📊 Inplay count: {len(events)}", flush=True)

    for ev in events:
        try:
            event_id = ev.get("id") or ev.get("event_id") or ev.get("FI") or ev.get("EV")
            home     = ev.get("home", ev.get("home_team", "Home"))
            away     = ev.get("away", ev.get("away_team", "Away"))
            league   = ev.get("league", ev.get("league_name") or ev.get("league_str") or "League")
            country  = ev.get("cc", ev.get("country") or ev.get("country_name") or "—")
            minute   = ev.get("timer", ev.get("time", ev.get("match_time", "N/A")))
            score    = ev.get("ss") or f"{ev.get('home_score','?')}-{ev.get('away_score','?')}"

            min_i = to_int_minute(minute)
            gh, ga = None, None
            if isinstance(score, str) and "-" in score:
                try:
                    gh, ga = [int(x) for x in score.split("-", 1)]
                except: 
                    gh = ev.get("home_score"); ga = ev.get("away_score")

            print(f"   · {country} — {league}, {minute}' | {home} {score} {away} | id={event_id}", flush=True)

            if not event_id:
                continue

            view = fetch_event_view(event_id)
            if not isinstance(view, dict):
                print("      ↪ no event_view json", flush=True)
                continue
            payload = view.get("results") or view.get("data") or view

            stats_home = try_extract_basic_stats(payload, team_side="home")
            stats_away = try_extract_basic_stats(payload, team_side="away")
            reds_h, reds_a = count_red_cards_from_events(payload)

            # Debug line
            print(f"      ↪ H SOT={stats_home['SOT']} Shots={stats_home['Shots']} Corners={stats_home['Corners']} xG={stats_home['xG']} | A SOT={stats_away['SOT']} Shots={stats_away['Shots']} Corners={stats_away['Corners']} xG={stats_away['xG']} | Reds H/A={reds_h}/{reds_a}", flush=True)

            # אם אין דקת משחק או מעל 60 — אל תתריע
            if min_i is None or min_i > MAX_ALERT_MINUTE:
                continue

            # ברירת מחדל תוצאה אם לא ידועה
            gh2 = gh if isinstance(gh, int) else (ev.get("home_score") or 0)
            ga2 = ga if isinstance(ga, int) else (ev.get("away_score") or 0)

            # כללי התראות לכל קבוצה בנפרד:
            def rules_for(team_name, team_stats, goals, opp_name, is_away):
                prefix = f"{country} — {league}, {min_i}' • {home} {gh2}-{ga2} {away}"
                # xG
                if isinstance(team_stats["xG"], (int,float)) and team_stats["xG"] >= XG_THRESHOLD and (goals or 0) == 0:
                    key = make_key(event_id, team_name, "xg")
                    if key not in sent_alerts:
                        tgsend_all(f"📈 {prefix}\n{team_name} xG={team_stats['xG']:.2f} but 0 goals vs {opp_name}.")
                        sent_alerts.add(key)
                # SOT
                if isinstance(team_stats["SOT"], (int,float)) and team_stats["SOT"] >= SOT_THRESHOLD and (goals or 0) == 0:
                    key = make_key(event_id, team_name, "sot")
                    if key not in sent_alerts:
                        tgsend_all(f"🎯 {prefix}\n{team_name} has {int(team_stats['SOT'])} shots on target with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)
                # Corners
                if isinstance(team_stats["Corners"], (int,float)) and team_stats["Corners"] >= CORNERS_THRESHOLD and (goals or 0) == 0:
                    key = make_key(event_id, team_name, "corners")
                    if key not in sent_alerts:
                        tgsend_all(f"🚩 {prefix}\n{team_name} has {int(team_stats['Corners'])} corners with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)
                # Total shots
                if isinstance(team_stats["Shots"], (int,float)) and team_stats["Shots"] >= TOTAL_SHOTS_THRESHOLD and (goals or 0) == 0:
                    key = make_key(event_id, team_name, "shots")
                    if key not in sent_alerts:
                        tgsend_all(f"📸 {prefix}\n{team_name} has {int(team_stats['Shots'])} total shots with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)
                # Red card (away)
                if is_away and isinstance(reds_a, (int,float)) and int(reds_a) >= 1:
                    key = make_key(event_id, team_name, "red_away")
                    if key not in sent_alerts:
                        tgsend_all(f"🟥 {prefix}\nRed card to AWAY team {team_name} vs {opp_name}.")
                        sent_alerts.add(key)

            # הפעלת כללים לשתי הקבוצות
            rules_for(home, stats_home, gh2, away, is_away=False)
            rules_for(away, stats_away, ga2, home, is_away=True)

        except Exception as e:
            print(f"❌ Exception processing event: {e}", flush=True)

# ============== MAIN LOOP ==============
if __name__ == "__main__":
    mask = BETSAPI_TOKEN[:4] + "…" + BETSAPI_TOKEN[-4:] if len(BETSAPI_TOKEN) >= 8 else BETSAPI_TOKEN
    print("🔑 Using BetsAPI token:", mask, flush=True)
    print("📬 Telegram destinations:", ", ".join([cid for _, cid in DESTINATIONS]) or "none", flush=True)
    if DESTINATIONS:
        tgsend_all("✅ Bot started · BetsAPI-only · heartbeat 10h · watchdog 15m")

    while True:
        try:
            scan_once()
            maybe_heartbeat(False)
            watchdog_ping()
        except Exception as e:
            print(f"❌ Uncaught error in loop: {e}", flush=True)
            tgsend_all(f"❌ Uncaught error in loop: {e}")
            time.sleep(5)
        time.sleep(SCAN_INTERVAL_SEC)
