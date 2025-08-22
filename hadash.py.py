import time
import requests
from datetime import datetime, timezone

# ========== Your Keys ==========
API_TOKEN        = "5a932cc6f53d4a61b4d06e6f21c98e69"  # X-Auth-Token provider
TELEGRAM_TOKEN   = "7846015183:AAGam93j9_FeRbUEfN6pNPLxoIbJC9fjVfc"
TELEGRAM_CHAT_ID = "6468640776"

# ========== Config ==========
SCAN_INTERVAL_SEC = 120       # every 2 minutes (24/7)
TIMEOUT_SEC       = 20
ENABLE_TELEGRAM_ALERTS = True # set False if you don't want Telegram notices

# track which live matches we already announced
announced_live_matches = set()   # contains match IDs

# ---------- HTTP helpers ----------
def fd_headers():
    return {"X-Auth-Token": API_TOKEN}

def send_telegram(text: str):
    if not ENABLE_TELEGRAM_ALERTS:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.status_code != 200:
            print(f"⚠️ Telegram {r.status_code}: {r.text[:200]}")
        else:
            print("✅ Telegram sent.")
    except Exception as e:
        print("⚠️ Telegram exception:", e)

# ---------- Football-Data API ----------
def fetch_live_matches():
    """
    Query live matches.
    Docs-style endpoint example: /v4/matches?status=LIVE
    """
    url = "https://api.football-data.org/v4/matches"
    params = {"status": "LIVE"}  # IN_PLAY/LIVE per provider; LIVE works in v4
    try:
        r = requests.get(url, headers=fd_headers(), params=params, timeout=TIMEOUT_SEC)
    except Exception as e:
        print("❌ Network error:", e)
        return []

    if r.status_code != 200:
        print(f"❌ HTTP {r.status_code}: {r.text[:300]}")
        return []

    try:
        js = r.json()
    except Exception:
        print("❌ Non-JSON response:", r.text[:300])
        return []

    return (js.get("matches") or [])

def parse_iso_utc(iso_str: str):
    """Parse '2025-08-22T18:45:00Z' -> aware datetime (UTC)."""
    if not iso_str:
        return None
    try:
        if iso_str.endswith("Z"):
            iso_str = iso_str.replace("Z", "+00:00")
        return datetime.fromisoformat(iso_str).astimezone(timezone.utc)
    except Exception:
        return None

def elapsed_minutes_from_kickoff(utc_kickoff):
    """Estimate elapsed minutes from kickoff to now (rough, not stoppage-aware)."""
    if not utc_kickoff:
        return "N/A"
    now_utc = datetime.now(timezone.utc)
    mins = int((now_utc - utc_kickoff).total_seconds() // 60)
    if mins < 0: mins = 0
    # cap to 130 just to avoid crazy values when extra/penalties etc.
    if mins > 130: mins = 130
    return mins

# ---------- Main scan ----------
def scan_once():
    print("🔄 Starting new scan...")
    matches = fetch_live_matches()
    print(f"📊 Found {len(matches)} live matches")

    for m in matches:
        match_id   = m.get("id")
        comp_name  = (m.get("competition") or {}).get("name", "Unknown Competition")
        home_name  = (m.get("homeTeam") or {}).get("name", "Home")
        away_name  = (m.get("awayTeam") or {}).get("name", "Away")
        status     = m.get("status", "UNKNOWN")
        utcDate    = parse_iso_utc(m.get("utcDate"))
        minute_est = elapsed_minutes_from_kickoff(utcDate)

        score = m.get("score") or {}
        full_time = score.get("fullTime") or {}
        half_time = score.get("halfTime") or {}
        # live score is generally reflected in fullTime when not finished? Sometimes in "score" top-level.
        # We'll try 'score' current fields first:
        home_goals = (score.get("fullTime") or {}).get("home")
        away_goals = (score.get("fullTime") or {}).get("away")

        # if None, try a fallback commonly seen: score["fullTime"] may be None until FT.
        if home_goals is None or away_goals is None:
            # Try 'halfTime' first, else 0
            home_goals = (half_time.get("home") if isinstance(half_time.get("home"), int) else 0)
            away_goals = (half_time.get("away") if isinstance(half_time.get("away"), int) else 0)

        # pretty print
        minute_str = f"{minute_est}'" if isinstance(minute_est, int) else "N/A"
        print(f"   · {comp_name}, {minute_str} | {home_name} {home_goals}-{away_goals} {away_name} | status={status}")

        # announce newly-seen live matches (once)
        if match_id and match_id not in announced_live_matches:
            announced_live_matches.add(match_id)
            msg = (f"📣 LIVE match detected\n"
                   f"{comp_name}, {minute_str}\n"
                   f"{home_name} {home_goals}-{away_goals} {away_name}")
            send_telegram(msg)

# =================== Runner (24/7) ===================
if __name__ == "__main__":
    print("✅ Using X-Auth-Token:", API_TOKEN[:4] + "…" + API_TOKEN[-4:])
    while True:
        try:
            scan_once()
        except Exception as e:
            print("❌ Uncaught error in scan:", e)
            time.sleep(5)
        time.sleep(SCAN_INTERVAL_SEC)
