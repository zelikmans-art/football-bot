import time
import requests
from datetime import datetime
import pytz

# ====== CONFIG ======
API_TOKEN = "236044-vjHdM29EvfZhfx"
TELEGRAM_TOKENS = [
    ("7846015183:AAFe_th1p1MehiqccHTWbVdDrtdLhYlEUro", "6468640776"),  # שלך
    ("8274943212:AAF8Vq20c3LcyB4zMir4QT_B9lBM41z7dYg", "1493637263")   # בוט שני
]
TIMEZONE = pytz.timezone("Asia/Jerusalem")
BET_URL = "https://api.betsapi.com/v1/bet365"
SCAN_INTERVAL_DAY = 30        # כל 30 שניות ביום
SCAN_INTERVAL_NIGHT = 480     # כל 8 דקות בלילה
STOP_SCAN_MINUTE = 60 + 5     # לא מעל דקה 65
HEARTBEAT_INTERVAL = 36000    # כל 10 שעות

# ====== Telegram ======
def send_telegram(message):
    for token, chat_id in TELEGRAM_TOKENS:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {"chat_id": chat_id, "text": message}
            r = requests.post(url, data=data, timeout=10)
            if r.status_code == 200:
                print(f"✅ Sent to {chat_id}")
            else:
                print(f"⚠️ Telegram {chat_id}: {r.text}")
        except Exception as e:
            print(f"⚠️ Telegram send error: {e}")

# ====== Fetch in-play ======
def fetch_inplay():
    url = f"{BET_URL}/inplay?sport_id=1&token={API_TOKEN}"
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        if data.get("success") == 1:
            return data["results"]
        else:
            print(f"⚠️ No success: {data}")
            return []
    except Exception as e:
        print(f"❌ Error fetch_inplay: {e}")
        return []

# ====== Fetch stats per match ======
def fetch_event_stats(ev_id):
    url = f"{BET_URL}/event?FI={ev_id}&stats=1&token={API_TOKEN}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("success") == 1 and "results" in data:
            return data["results"]
        return None
    except Exception as e:
        print(f"❌ fetch_event_stats({ev_id}): {e}")
        return None

# ====== Parse and check triggers ======
def check_triggers(ev, stats):
    try:
        home = stats[0]
        away = stats[1]

        home_name = ev.get("HOME", "?")
        away_name = ev.get("AWAY", "?")
        score = f"{ev.get('SS', '0-0')}"
        minute = int(ev.get("TIME", 0) or 0)

        if minute > STOP_SCAN_MINUTE:
            return None

        def val(obj, key):
            v = obj.get(key, "")
            try:
                return int(v)
            except:
                return 0

        # ערכים רלוונטיים
        home_sot = val(home, "S1")
        away_sot = val(away, "S1")
        home_shots = val(home, "S2") + val(home, "S1")
        away_shots = val(away, "S2") + val(away, "S1")
        home_corners = val(home, "S11")
        away_corners = val(away, "S11")
        home_reds = 1 if home.get("RC") == "1" else 0
        away_reds = 1 if away.get("RC") == "1" else 0
        home_xg = float(home.get("XG", 0) or 0)
        away_xg = float(away.get("XG", 0) or 0)
        home_key = val(home, "KP") if "KP" in home else 0
        away_key = val(away, "KP") if "KP" in away else 0
        home_yellows = val(home, "YC")
        away_yellows = val(away, "YC")

        msg = None
        # ===== תנאי ההתראות =====
        if (home_sot >= 6 and "0" in score.split("-")[1]) or (away_sot >= 6 and "0" in score.split("-")[0]):
            msg = f"⚽ {home_name} vs {away_name}: 6+ shots on target, no goal yet."
        elif (home_shots >= 12 and "0" in score.split("-")[1]) or (away_shots >= 12 and "0" in score.split("-")[0]):
            msg = f"🔥 {home_name} vs {away_name}: 12 total shots, no goal yet."
        elif (home_corners >= 8 and "0" in score.split("-")[1]) or (away_corners >= 8 and "0" in score.split("-")[0]):
            msg = f"🏁 {home_name} vs {away_name}: 8+ corners, no goal yet."
        elif (home_xg >= 1 and "0" in score.split("-")[1]) or (away_xg >= 1 and "0" in score.split("-")[0]):
            msg = f"📊 {home_name} vs {away_name}: XG ≥ 1.0 but no goal yet."
        elif (home_key >= 6 and "0" in score.split("-")[1]) or (away_key >= 6 and "0" in score.split("-")[0]):
            msg = f"🎯 {home_name} vs {away_name}: 6+ key passes, no goal yet."
        elif home_reds or away_reds:
            msg = f"🟥 {home_name} vs {away_name}: Red card!"
        elif minute == 45 and home_yellows + away_yellows == 0:
            msg = f"⚠️ {home_name} vs {away_name}: 0 yellow cards at halftime."

        if msg:
            send_telegram(msg)
            print(msg)
    except Exception as e:
        print(f"❌ Trigger check failed: {e}")

# ====== MAIN LOOP ======
def main():
    last_heartbeat = time.time()
    print(f"🔑 Using Bet365 API token: {API_TOKEN[:4]}…{API_TOKEN[-4:]}")
    while True:
        now = datetime.now(TIMEZONE)
        hour = now.hour
        minute = now.minute

        if 9 <= hour < 23 or (hour == 23 and minute <= 30):
            interval = SCAN_INTERVAL_DAY
        else:
            interval = SCAN_INTERVAL_NIGHT

        print(f"\n🔄 Scan @ {now.strftime('%Y-%m-%d %H:%M:%S')} | interval={interval}s")
        results = fetch_inplay()
        if results:
            evs = []
            for block in results:
                if isinstance(block, list):
                    evs.extend(block)
                else:
                    evs.append(block)
            print(f"📊 INPLAY count: {len(evs)}")

            for ev in evs[:30]:
                ev_id = ev.get("ID")
                if not ev_id:
                    continue
                stats = fetch_event_stats(ev_id)
                if stats:
                    check_triggers(ev, stats)
        else:
            print("ℹ️ No inplay results.")

        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
            send_telegram("✅ Heartbeat: bot still running.")
            last_heartbeat = time.time()

        time.sleep(interval)

if __name__ == "__main__":
    main()
