import time
import os
import requests
from datetime import datetime

# ========= API key =========
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "a7c81be8b4938a51d686b8ebd18c5242")

# ========= Telegram destinations =========
# Primary bot (your existing one)
PRIMARY_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "7846015183:AAGam93j9_FeRbUEfN6pNPLxoIbJC9fjVfc")
PRIMARY_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6468640776")

# Extra bot (the new one you provided)
EXTRA_TOKEN   = os.getenv("TELEGRAM_EXTRA_TOKEN",   "8274943212:AAF8Vq20c3LcyB4zMir4QT_B9lBM41z7dYg")
EXTRA_CHAT_ID = os.getenv("TELEGRAM_EXTRA_CHAT_ID", "1493637263")

# Optional: arbitrary list "TOKEN1|CHATID1,TOKEN2|CHATID2,..."
TELEGRAM_DESTINATIONS = os.getenv("TELEGRAM_DESTINATIONS", "").strip()

def _build_destinations():
    dests = []
    if PRIMARY_TOKEN and PRIMARY_CHAT_ID:
        dests.append((PRIMARY_TOKEN, PRIMARY_CHAT_ID))
    if EXTRA_TOKEN and EXTRA_CHAT_ID:
        dests.append((EXTRA_TOKEN, EXTRA_CHAT_ID))
    if TELEGRAM_DESTINATIONS:
        for part in TELEGRAM_DESTINATIONS.split(","):
            part = part.strip()
            if not part or "|" not in part:
                continue
            tok, cid = part.split("|", 1)
            tok = tok.strip(); cid = cid.strip()
            if tok and cid:
                dests.append((tok, cid))
    # dedupe
    uniq, seen = [], set()
    for t, c in dests:
        k = f"{t}:{c}"
        if k not in seen:
            uniq.append((t, c)); seen.add(k)
    return uniq

DESTINATIONS = _build_destinations()

# ========= Config =========
SCAN_INTERVAL_SEC        = 120   # every 2 minutes (24/7)
TIMEOUT_SEC              = 20
# thresholds
XG_THRESHOLD             = 0.8
SOT_THRESHOLD            = 4
CORNERS_THRESHOLD        = 6
TOTAL_SHOTS_THRESHOLD    = 8
MAX_ALERT_MINUTE         = 60    # don't alert after 60'
# heartbeat
HEARTBEAT_EVERY_SEC      = 10 * 60 * 60  # 10 hours
start_time               = datetime.utcnow()
last_heartbeat_ts        = 0.0
scan_count               = 0
last_scan_time           = None

# de-dup
sent_alerts = set()  # "fixtureId:Team:rule"

# ---------- Telegram ----------
def send_telegram_all(text: str):
    if not DESTINATIONS:
        print("⚠️ No Telegram destinations configured."); return
    for token, chat_id in DESTINATIONS:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
            if r.status_code != 200:
                print(f"⚠️ Telegram {chat_id} {r.status_code}: {r.text[:300]}")
            else:
                print(f"✅ Telegram sent to {chat_id}.")
        except Exception as e:
            print(f"❌ Telegram exception to {chat_id}:", e)

def maybe_send_heartbeat(force: bool = False):
    global last_heartbeat_ts, scan_count, last_scan_time
    now_ts = time.time()
    if force or (now_ts - last_heartbeat_ts >= HEARTBEAT_EVERY_SEC):
        uptime = datetime.utcnow() - start_time
        uptime_str = str(uptime).split(".")[0]
        last_scan_str = last_scan_time.strftime("%Y-%m-%d %H:%M:%S UTC") if last_scan_time else "N/A"
        dests_str = ", ".join([cid for _, cid in DESTINATIONS]) if DESTINATIONS else "none"
        send_telegram_all(
            "✅ Heartbeat\n"
            f"Uptime: {uptime_str}\n"
            f"Scans: {scan_count}\n"
            f"Last scan: {last_scan_str}\n"
            f"Destinations: {dests_str}"
        )
        last_heartbeat_ts = now_ts

# ---------- API-Football ----------
def af_headers():
    return {"x-apisports-key": API_FOOTBALL_KEY}

def get_json_with_debug(url, kind, params=None):
    try:
        r = requests.get(url, headers=af_headers(), params=params, timeout=TIMEOUT_SEC)
    except Exception as e:
        print(f"❌ Network exception ({kind}):", e); return None
    print(f"🌐 {kind} HTTP={r.status_code} url={url}")
    if r.status_code == 429:
        print("⏳ Rate limited (429). Back off 30s."); time.sleep(30); return None
    ctype = r.headers.get("content-type","")
    if not ctype.startswith("application/json"):
        print(f"❌ Non-JSON {kind}: {r.text[:300]}"); return None
    try:
        js = r.json()
    except Exception:
        print(f"❌ JSON parse error ({kind}): {r.text[:300]}"); return None
    if isinstance(js, dict):
        print("ℹ️", kind, "results:", js.get("results"), "| errors:", js.get("errors"))
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

def find_value(stats_list, team_name, candidates):
    for block in stats_list:
        if (block.get("team") or {}).get("name") != team_name:
            continue
        for row in (block.get("statistics") or []):
            typ = str(row.get("type", "")).lower()
            if not any(c in typ for c in candidates): continue
            val = row.get("value")
            if isinstance(val, (int, float)): return float(val)
            if isinstance(val, str):
                s = val.strip().rstrip("%")
                try: return float(s)
                except: pass
    return None

def count_red_cards(events, team_name):
    cnt = 0
    for ev in events:
        t = (ev.get("team") or {}).get("name")
        etype = (ev.get("type") or "").lower()
        edet  = (ev.get("detail") or "").lower()
        if t == team_name and ("red card" in edet or (etype == "card" and "red" in edet)):
            cnt += 1
    return cnt

def make_key(fixture_id, team, rule):
    return f"{fixture_id}:{team}:{rule}"

def to_int_minute(minute):
    try:
        if isinstance(minute, (int, float)): return int(minute)
        if isinstance(minute, str): return int(minute.strip().replace("'", ""))
    except: return None
    return None

# ---------- Main scan ----------
def scan_once():
    global scan_count, last_scan_time
    last_scan_time = datetime.utcnow()
    scan_count += 1

    print(f"🔄 Scan @ {last_scan_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    fixtures = get_live_fixtures()
    print(f"📊 Found {len(fixtures)} live games")

    for g in fixtures:
        try:
            fixture   = g.get("fixture", {})
            league    = g.get("league", {})
            teams     = g.get("teams", {})
            goals     = g.get("goals", {})
            status    = (fixture.get("status") or {})
            minute    = status.get("elapsed", "N/A")
            minute_int = to_int_minute(minute)

            league_name   = league.get("name", "Unknown League")
            league_country= league.get("country", "Unknown Country")
            fixture_id    = fixture.get("id")

            home = (teams.get("home") or {}).get("name", "Home")
            away = (teams.get("away") or {}).get("name", "Away")
            gh   = goals.get("home", 0) or 0
            ga   = goals.get("away", 0) or 0

            stats  = get_fixture_stats(fixture_id)
            events = get_fixture_events(fixture_id)

            if not stats and not events:
                print(f"ℹ️ No stats/events yet for {fixture_id} | {league_country} — {league_name}, {minute}' | {home} vs {away}")
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

                dbg = {
                    "xG": f"{xg:.2f}" if isinstance(xg,(int,float)) else "N/A",
                    "SOT": f"{int(sot)}" if isinstance(sot,(int,float)) else "N/A",
                    "Shots": f"{int(tot)}" if isinstance(tot,(int,float)) else "N/A",
                    "Corners": f"{int(crn)}" if isinstance(crn,(int,float)) else "N/A",
                    "Reds": f"{int(reds)}" if isinstance(reds,(int,float)) else "0",
                }
                print(f"   · {league_country} — {league_name}, {minute}' | {team_name}: {dbg} | score={home} {gh}-{ga} {away}")

                # Do not alert after minute > 60
                if minute_int is None or minute_int > MAX_ALERT_MINUTE:
                    continue

                score_str = f"{home} {gh}-{ga} {away}"
                prefix = f"{league_country} — {league_name}, {minute}' • {score_str}"

                # A) xG >= 0.8 and 0 goals
                if isinstance(xg,(int,float)) and xg >= XG_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, f"xg_ge_{XG_THRESHOLD}_no_goal")
                    if key not in sent_alerts:
                        send_telegram_all(f"📈 {prefix}\n{team_name} xG = {xg:.2f} with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # B) SOT >= 4 and 0 goals
                if isinstance(sot,(int,float)) and sot >= SOT_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, f"shotsot_ge_{SOT_THRESHOLD}_no_goal")
                    if key not in sent_alerts:
                        send_telegram_all(f"🎯 {prefix}\n{team_name} has {int(sot)} shots on target with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # C) Red card for the AWAY team
                if is_away and isinstance(reds,(int,float)) and int(reds) >= 1:
                    key = make_key(fixture_id, team_name, "red_card_away")
                    if key not in sent_alerts:
                        send_telegram_all(f"🟥 {prefix}\nRed card to AWAY team {team_name} vs {opp_name}.")
                        sent_alerts.add(key)

                # D) Corners >= 6 and 0 goals
                if isinstance(crn,(int,float)) and crn >= CORNERS_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, f"corners_ge_{CORNERS_THRESHOLD}_no_goal")
                    if key not in sent_alerts:
                        send_telegram_all(f"🚩 {prefix}\n{team_name} has {int(crn)} corners with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # E) Total shots >= 8 and 0 goals
                if isinstance(tot,(int,float)) and tot >= TOTAL_SHOTS_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, f"totalshots_ge_{TOTAL_SHOTS_THRESHOLD}_no_goal")
                    if key not in sent_alerts:
                        send_telegram_all(f"📸 {prefix}\n{team_name} has {int(tot)} total shots with 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

        except Exception as e:
            print("❌ Exception processing fixture:", e)

# =================== Runner (24/7) ===================
if __name__ == "__main__":
    print("🔑 API_FOOTBALL_KEY in use:", API_FOOTBALL_KEY[:4] + "…" + API_FOOTBALL_KEY[-4:])
    send_telegram_all("✅ Bot started (API-Football stats alerts, heartbeat 10h, multi-bot)")
    maybe_send_heartbeat(force=True)

    while True:
        try:
            scan_once()
            maybe_send_heartbeat(force=False)
        except Exception as e:
            print("❌ Uncaught error in loop:", e)
            time.sleep(5)
        time.sleep(SCAN_INTERVAL_SEC)
