import requests
import json

API_TOKEN = "236044-vjHdM29EvfZhfx"

def test_betsapi():
    url = f"https://api.b365api.com/v3/events/inplay?sport_id=1&token={API_TOKEN}"
    print(f"🔍 Testing BetsAPI live endpoint:\n{url}\n")

    try:
        resp = requests.get(url, timeout=15)
        print(f"🌐 HTTP status: {resp.status_code}")
        if resp.status_code != 200:
            print("❌ Request failed, raw text:")
            print(resp.text)
            return

        data = resp.json()
        print("✅ JSON parsed successfully.")
        print("keys:", list(data.keys()))

        results = data.get("results") or []
        print(f"📊 Found {len(results)} live events.")
        if results:
            sample = results[0]
            print("\n📘 Sample event:")
            print(json.dumps(sample, indent=2)[:1000])
        else:
            print("⚠️ No live data returned. Could be no games or permission issue.")

    except Exception as e:
        print("💥 Exception:", e)

if __name__ == "__main__":
    test_betsapi()
