from kiteconnect import KiteConnect

# same creds as server.py
api_key = "0r1dt27vy4vqg86q"
api_secret = "3p5f50cd717o35vo4t5cto2714fpn1us"

kite = KiteConnect(api_key=api_key)

print("1️⃣ Open this URL in browser and log in to Zerodha using the SAME account:")
print(kite.login_url())
print()
print("2️⃣ After login, Zerodha will redirect you to your Redirect URL:")
print("   http://127.0.0.1:8000/?request_token=XXXXXXXX&action=login&type=login")
print()
request_token = input("Paste ONLY the request_token value here: ").strip()

session_data = kite.generate_session(request_token, api_secret=api_secret)

access_token = session_data["access_token"]

print("\n✅ ACCESS_TOKEN (copy this):")
print(access_token)
print("\nNow run this in a SEPARATE terminal tab (while uvicorn is running):\n")
print(f'curl -X POST "http://127.0.0.1:8000/admin/set_token" \\')
print(f'  -H "Content-Type: application/json" \\')
print(f'  -H "X-ADMIN-KEY: HedgeAI_Admin_2025" \\')
print(f'  -d \'{{"access_token":"{access_token}"}}\'')