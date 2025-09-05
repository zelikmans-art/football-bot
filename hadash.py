import time
import os
import requests
from datetime import datetime

# ========= Keys (אפשר לשים כ-ENV ב-Render, ואם לא קיים—נשתמש בקשיח) =========
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "a7c81be8b4938a51d686b8ebd18c5242")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "7846015183:AAGam93j9_FeRbUEfN6pNPLxoIbJC9fjVfc")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6468640776")

# ========= Config =========
SCAN_INTERVAL_SEC  = 120    # every 2 minutes (24/7)
TIMEOUT_SEC        = 20

# thresholds
XG_THRESHOLD       = 1.5
SOT_THRESHOLD      = 7

# de-dup alerts
sent_alerts = set()  # "fixtureId:Team:rule"

# ---------- Telegram ----------
def send_telegram(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.status_code != 200:
            print(f"⚠️ Telegram {r.status_code}: {r.text[:300]}")
        else:
            print("✅ Telegram sent.")
    except Exception as e:
        print("❌ Telegram exception:", e)

# ---------- API-Football helpers ----------
def af_headers():
    return {"x-apisports-key": API_FOOTBALL_KEY}

def get_json_with_debug(url, kind, params=None):
    """Generic GET with debug prints for HTTP code + API 'results'/'errors' fields."""
    try:
        r = requests.get(url, headers=af_headers(), params=params, timeout=TIMEOUT_SEC)
    except Exception as e:
        print(f"❌ Network exception ({kind}):", e)
        return None
    print(f"🌐 {kind} HTTP={r.status_code} url={url}")
    ctype = r.headers.get("content-type","")
    if r.status_code == 429:
        print("⏳ Rate limited (429). Backing off 30s.")
        time.sleep(30)
        return None
    if not ctype.startswith("application/json"):
        print(f"❌ Non-JSON {kind}: {r.text[:300]}")
        return None
    try:
        js = r.json()
    except Exception:
        print(f"❌ JSON parse error ({kind}): {r.text[:300]}")
        return None
    # Debug fields if present
    if isinstance(js, dict):
        print("ℹ️", kind, "results:", js.get("results"), "| errors:", js.get("errors"))
    return js

def get_live_fixtures():
    js = get_json_with_debug("https://v3.football.api-sports.io/fixtures?live=all", "fixtures")
    if not isinstance(js, dict):
        return []
    return js.get("response") or []

def get_fixture_stats(fixture_id: int):
    js = get_json_with_debug(
        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}",
        f"stats fixture={fixture_id}"
    )
    if not isinstance(js, dict):
        return []
    return js.get("response") or []

def get_fixture_events(fixture_id: int):
    js = get_json_with_debug(
        f"https://v3.football.api-sports.io/fixtures/events?fixture={fixture_id}",
        f"events fixture={fixture_id}"
    )
    if not isinstance(js, dict):
        return []
    return js.get("response") or []

def find_value(stats_list, team_name, candidates):
    """Find numeric stat for team_name where 'type' contains any candidate (case-insensitive)."""
    for team_block in stats_list:
        if (team_block.get("team") or {}).get("name") != team_name:
            continue
        for row in (team_block.get("statistics") or []):
            typ = str(row.get("type", "")).lower()
            if not any(c in typ for c in candidates):
                continue
            val = row.get("value")
            # coerce numeric
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                s = val.strip().rstrip("%")
                try:
                    return float(s)
                except:
                    pass
    return None

def count_red_cards(events, team_name):
    """Count red cards for a specific team using the events feed."""
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

# ---------- Main scan ----------
def scan_once():
    print(f"🔄 Scan @ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
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
            league_name = league.get("name", "Unknown League")
            fixture_id  = fixture.get("id")

            home = (teams.get("home") or {}).get("name", "Home")
            away = (teams.get("away") or {}).get("name", "Away")
            gh   = goals.get("home", 0) or 0
            ga   = goals.get("away", 0) or 0

            # Pull stats & events (with debug printed inside)
            stats  = get_fixture_stats(fixture_id)
            events = get_fixture_events(fixture_id)

            if not stats and not events:
                print(f"ℹ️ No stats/events yet for {fixture_id} | {league_name}, {minute}' | {home} vs {away}")
                continue

            # per team metrics
            for team_name, team_goals, opp_name, is_away in (
                (home, gh, away, False),
                (away, ga, home, True),
            ):
                xg  = find_value(stats, team_name, ["expected goals", "xg"])
                sot = find_value(stats, team_name, ["shots on goal", "shots on target"])
                tot = find_value(stats, team_name, ["total shots", "shots total", "shots"])
                crn = find_value(stats, team_name, ["corner kicks", "corners"])
                reds = count_red_cards(events, team_name)

                dbg_xg  = f"{xg:.2f}" if isinstance(xg,(int,float)) else "N/A"
                dbg_sot = f"{int(sot)}" if isinstance(sot,(int,float)) else "N/A"
                dbg_tot = f"{int(tot)}" if isinstance(tot,(int,float)) else "N/A"
                dbg_crn = f"{int(crn)}" if isinstance(crn,(int,float)) else "N/A"
                dbg_red = f"{int(reds)}" if isinstance(reds,(int,float)) else "0"

                print(f"   · {league_name}, {minute}' | {team_name}: SOT={dbg_sot} | xG={dbg_xg} | Shots={dbg_tot} | Corners={dbg_crn} | Reds={dbg_red} | score={home} {gh}-{ga} {away}")

                # ===== Alerts =====
                score_str = f"{home} {gh}-{ga} {away}"
                prefix = f"{league_name}, {minute}' • {score_str}"

                # A) xG > 1.5 and 0 goals
                if isinstance(xg,(int,float)) and xg > XG_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, f"xg_gt_{XG_THRESHOLD}_no_goal")
                    if key not in sent_alerts:
                        send_telegram(f"📈 {prefix}\n{team_name} xG = {xg:.2f} but 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # B) SOT >= 7 and 0 goals
                if isinstance(sot,(int,float)) and sot >= SOT_THRESHOLD and team_goals == 0:
                    key = make_key(fixture_id, team_name, "shots7_no_goal")
                    if key not in sent_alerts:
                        send_telegram(f"🎯 {prefix}\n{team_name} has {int(sot)} shots on target but 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # C) Red card for the AWAY team (events)
                if is_away and isinstance(reds,(int,float)) and int(reds) >= 1:
                    key = make_key(fixture_id, team_name, "red_card_away")
                    if key not in sent_alerts:
                        send_telegram(f"🟥 {prefix}\nRed card to AWAY team {team_name} vs {opp_name}.")
                        sent_alerts.add(key)

                # D) Corners > 10 and 0 goals
                if isinstance(crn,(int,float)) and crn > 10 and team_goals == 0:
                    key = make_key(fixture_id, team_name, "corners_gt10_no_goal")
                    if key not in sent_alerts:
                        send_telegram(f"🚩 {prefix}\n{team_name} has {int(crn)} corners but 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

                # E) Total shots > 14 and 0 goals
                if isinstance(tot,(int,float)) and tot > 14 and team_goals == 0:
                    key = make_key(fixture_id, team_name, "totalshots_gt14_no_goal")
                    if key not in sent_alerts:
                        send_telegram(f"📸 {prefix}\n{team_name} has {int(tot)} total shots but 0 goals vs {opp_name}.")
                        sent_alerts.add(key)

        except Exception as e:
            print("❌ Exception processing fixture:", e)

# =================== Runner (24/7) ===================
if __name__ == "__main__":
    print("🔑 API_FOOTBALL_KEY in use:", API_FOOTBALL_KEY[:4] + "…" + API_FOOTBALL_KEY[-4:])
    send_telegram("✅ Bot started (API-Football stats alerts enabled, with diagnostics)")
    while True:
        try:
            scan_once()
        except Exception as e:
            print("❌ Uncaught error in loop:", e)
            time.sleep(5)
        time.sleep(SCAN_INTERVAL_SEC)
