import os
import sys
import time
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================== CONFIG ==================
SCAN_INTERVAL_SEC      = 120        # every 2 minutes
TIMEOUT_SEC            = 20
HEARTBEAT_EVERY_SEC    = 10 * 60 * 60   # 10 hours
WATCHDOG_STALL_SEC     = 15 * 60        # alert if no progress for 15 min

# thresholds (כמו שסיכמנו)
XG_THRESHOLD           = 0.8
SOT_THRESHOLD          = 4
CORNERS_THRESHOLD      = 6
TOTAL_SHOTS_THRESHOLD  = 8
MAX_ALERT_MINUTE       = 60          # לא שולחים התראות אחרי דקה 60

# ================== ENV ==================
def get_env(name, default=""):
    return os.getenv(name, default).strip()

API_KEY = get_env("API_KEY")  # ← רק טוקן חדש פה
if not API_KEY:
    print("🚫 Missing API_KEY. Set it in Render → Environment.", flush=True)
    sys.exit(1)

# Telegram destinations
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
    print("🚫 No Telegram destinations. Set TELEGRAM_TOKEN & TELEGRAM_CHAT_ID (or DESTINATIONS / *_2).", flush=True)
    sys.exit(1)

# ================== STATE ==================
start_time        = datetime.now(timezone.utc)
last_heartbeat_ts = 0.0
last_progress_ts  = time.time()
scan_count        = 0
last_scan_time    = None
sent_alerts       = set()

# ================== HTTP SESSION (RETRIES) ==================
session = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ================== TELEGRAM ==================
def send_telegram_all(text: str):
    for token, chat_id in DESTINATIONS:
        try:
            r = session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=TIMEOUT_SEC
            )
            if r.status_code != 200:
                print(f"⚠️ Telegram {chat_id} {r.status_code}: {r.text[:300]}", flush=True)
            else:
                print(f"✅ Telegram sent to {chat_id}.", flush=True)
        except Exception as e:
            print(f"❌ Telegram exception to {chat_id}: {e}", flush=True)

def maybe_send_heartbeat(force=False):
    global last_heartbeat_ts
    now = time.time()
    if force or (now - last_heartbeat_ts >= HEARTBEAT_EVERY_SEC):
        uptime = str((datetime.now(timezone.utc) - start_time)).split(".")[0]
        last = last_scan_time.strftime("%Y-%m-%d %H:%M:%S UTC") if last_scan_time else "N/A"
        dests = ", ".join([cid for _, cid in DESTINATIONS]) or "none"
        send_telegram_all(f"✅ Heartbeat\nUptime: {uptime}\nScans: {scan_count}\nLast scan: {last}\nDestinations: {dests}")
        last_heartbeat_ts = now

def watchdog_ping():
    global last_progress_ts
    if time.time() - last_progress_ts >= WATCHDOG_STALL_SEC:
        send_telegram_all("⚠️ Watchdog: no scan progress in the last 15 minutes. Continuing…")
        last_progress_ts = time.time()

# ================== API-FOOTBALL (single key) ==================
def af_headers():
    return {"x-apisports-key": API_KEY}

def get_json_with_debug(url, kind, params=None):
    try:
        r = session.get(url, headers=af_headers(), params=params, timeout=TIMEOUT_SEC)
    except Exception as e:
        print(f"❌ Network exception ({kind}): {e}", flush=True)
        return None
    print(f"🌐 {kind} HTTP={r.status_code} url={url}", flush=True)
    if r.status_code == 429:
        print("⏳ Rate limited (429). Backoff 30s.", flush=True)
        time.sleep(30)
        return None
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("application/json"):
        print(f"❌ Non-JSON {kind}: {r.text[:300]}", flush=True)
        return None
    try:
        js = r.json()
    except Exception:
        print(f"❌ JSON parse error ({kind}): {r.text[:300]}", flush=True)
        return None
    if isinstance(js, dict):
        print("ℹ️", kind, "results:", js.get("results"), "| errors:", js.get("errors"), flush=True)
    return js

def get_live_fixtures():
    js = get_json_with_debug("https://v3.football.api-sports.io/fixtures?live=all", "fixtures")
    return js.get("response") if isinstance(js, dict) else []

def get_fixture_stats(fixture_id: int):
    js = get_json_with_debug(
        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}",
        f"stats fixture={fixture_id}"
    )
    return js.get("response") if isinstance(js, dict) else []

def get_fixture_events(fixture_id: int):
    js = get_json_with_debug(
        f"https://v3.football.api-sports.io/fixtures/events?fixture={fixture_id}",
        f"events fixture={fixture_id}"
    )
    return js.get("response") if isinstance(js, dict) else []

# ================== STAT HELPERS ==================
def find_value(stats_list, team_name, candidates):
    for block in stats_list or []:
        if (block.get("team") or {}).get("name") != team_name:
            continue
        for row in (block.get("statistics") or []):
            t = str(row.get("type","")).lower()
            if not any(c in t for c in candidates):
                continue
            v = row.get("value")
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                s = v.strip().rstrip("%")
                try:
                    return float(s)
                except:
                    pass
    return None

def count_red_cards(events, team_name):
    cnt = 0
    for ev in events or []:
        tname = (ev.get("team") or {}).get("name")
        typ   = (ev.get("type") or "").lower()
        det   = (ev.get("detail") or "").lower()
        if tname == team_name and ("red card" in det or (typ == "card" and "red" in det)):
            cnt += 1
    return cnt

def to_int_minute(minute):
    try:
        if isinstance(minute, (int, float)): return int(minute)
        if isinstance(minute, str): return int(minute.strip().replace("'", ""))
    except:
        return None
    return None

def make_key(fixture_id, team, rule):
    return f"{fixture_id}:{team}:{rule}"

# ================== SCAN ==================
def scan_once():
    global scan_count, last_scan_time, last_progress_ts
    last_scan_time = datetime.now(timezone.utc)
    scan_count += 1
    last_progress_ts = time.time()

    print(f"🔄 Scan @ {last_scan_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    fixtures = get_live_fixtures()
    print(f"📊 Found {len(fixtures)} live games", flush=True)

    for g in fixtures:
        try:
            fixture   = g.get("fixture", {})
            league    = g.get("league", {})
            teams     = g.get("teams", {})
            goals     = g.get("goals", {})
            status    = (fixture.get("status") or {})
            minute    = status.get("elapsed", "N/A")
            minute_i  = to_int_minute(minute)
            fixture_id = fixture.get("id")

            league_name    = league.get("name", "Unknown League")
            league_country = league.get("country", "Unknown Country")
            home = (teams.get("home") or {}).get("name", "Home")
            away = (teams.get("away") or {}).get("name", "Away")
            gh   = goals.get("home", 0) or 0
            ga   = goals.get("away", 0) or 0

            stats  = get_fixture_stats(fixture_id)
            events = get_fixture_events(fixture_id)

            if not stats and not events:
                print(f"ℹ️ No stats/events yet for {fixture_id} | {league_country} — {league_name}, {minute}' | {home} vs {away}", flush=True)
                continue

            for team_name, team_goals, opp_name, is_away in (
                (home, gh, away, False),
                (away, ga, home, True),
            ):
                xg  = find_value(stats, team_name, ["expected goals", "xg"])
                sot = find_value(stats, team_name, ["shots on goal", "shots on target"])
                tot = find_value(stats, team_name, ["total shots", "shots total", "shots"])
                crn = find_value(stats, team_name, ["corner kicks", "corners"])
                reds = count_red_cards(events, team_name)

                print(
                    f"   · {league_country} — {league_name}, {minute}' | "
                    f"{team_name}: SOT={sot if sot is not None else 'N/A'} | "
                    f"xG={f'{xg:.2f}' if isinstance(xg,(int,float)) else 'N/A'} | "
                    f"Shots={tot if tot is not None else 'N/A'} | "
                    f"Corners={crn if crn is not None else 'N/A'} | "
                    f"Reds={reds} | score={home} {gh}-{ga} {away}",
                    flush=True
                )

                if minute_i is None or minute_i > MAX_ALERT_MINUTE:
                    continue

                score_str = f"{home} {gh}-{ga} {away}"
                prefix = f"{league_country} — {league_name}, {minute}' • {score_str}"

                # xG rule
                if isinstance(xg,(int,float)) and xg >= XG_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, "xg_thresh")
                    if key not in sent_alerts:
                        send_telegram_all(f"📈 {prefix}\n{team_name} xG = {xg:.2f} with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # SOT rule
                if isinstance(sot,(int,float)) and sot >= SOT_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, "sot_thresh")
                    if key not in sent_alerts:
                        send_telegram_all(f"🎯 {prefix}\n{team_name} has {int(sot)} shots on target with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # Corners rule
                if isinstance(crn,(int,float)) and crn >= CORNERS_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, "corners_thresh")
                    if key not in sent_alerts:
                        send_telegram_all(f"🚩 {prefix}\n{team_name} has {int(crn)} corners with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # Total shots rule
                if isinstance(tot,(int,float)) and tot >= TOTAL_SHOTS_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, "totalshots_thresh")
                    if key not in sent_alerts:
                        send_telegram_all(f"📸 {prefix}\n{team_name} has {int(tot)} total shots with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # Red card away
                if is_away and isinstance(reds,(int,float)) and int(reds) >= 1:
                    key = make_key(fixture_id, team_name, "red_away")
                    if key not in sent_alerts:
                        send_telegram_all(f"🟥 {prefix}\nRed card to AWAY team {team_name} vs {opp_name}.")
                        sent_alerts.add(key)

        except Exception as e:
            print(f"❌ Exception processing fixture: {e}", flush=True)

# ================== MAIN ==================
if __name__ == "__main__":
    mask = API_KEY[:4] + "…" + API_KEY[-4:] if len(API_KEY) >= 8 else API_KEY
    print("🔑 API_KEY loaded:", mask, flush=True)
    print("📬 Telegram destinations:", ", ".join([cid for _, cid in DESTINATIONS]), flush=True)

    send_telegram_all("✅ Bot started · single-API (new token) · heartbeat 10h · watchdog 15m · retries+timeouts")
    maybe_send_heartbeat(force=True)

    while True:
        try:
            scan_once()
            maybe_send_heartbeat(False)
            watchdog_ping()
        except Exception as e:
            print(f"❌ Uncaught error in loop: {e}", flush=True)
            send_telegram_all(f"❌ Uncaught error in loop: {e}")
            time.sleep(5)
        time.sleep(SCAN_INTERVAL_SEC)
