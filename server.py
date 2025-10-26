import os
import time
import json
import threading
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# NEW: CORS so Shopify browser can call this API
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# ====== CONFIG ======
ADMIN_KEY = os.getenv("ADMIN_KEY", "HedgeAI_Admin_2025")
KITE_API_KEY = os.getenv("KITE_API_KEY", "0r1dt27vy4vqg86q")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "3p5f50cd717o35vo4t5cto2714fpn1us")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

SNAPSHOT_FILE = "last_snapshot.json"
ACCESS_FILE = "current_access_token.txt"

# ====== GLOBAL STATE ======
CACHE = {
    "cached_at": None,
    "spot": {},
    "chain": {"note": "option greeks / IV coming soon"},
    "stale": True,  # True means "this is last close / not live"
    "lot_sizes": {
        "NIFTY_50": 75,
        "BANKNIFTY": 35
    }
}

CURRENT_ACCESS_TOKEN = ""


# ====== FASTAPI APP ======
app = FastAPI()

# Allow calls from anywhere (Shopify store domain etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can later lock this to your Shopify domain
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokenBody(BaseModel):
    access_token: str


def load_snapshot_from_disk():
    """Load last known prices from disk into CACHE so weekend/offline still shows data."""
    global CACHE
    if not os.path.exists(SNAPSHOT_FILE):
        print("[SNAPSHOT] no snapshot file yet")
        return
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snap = json.load(f)
        CACHE["cached_at"] = snap.get("cached_at")
        CACHE["spot"] = snap.get("spot", {})
        CACHE["stale"] = True  # snapshot is always considered stale
        print("[SNAPSHOT LOADED]", CACHE["cached_at"], CACHE["spot"])
    except Exception as e:
        print("[SNAPSHOT ERROR]", e)


def save_snapshot_to_disk():
    """Save CACHE spot + timestamp so next boot (or weekend) has data."""
    try:
        snap = {
            "cached_at": CACHE["cached_at"],
            "spot": CACHE["spot"]
        }
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
        print("[SNAPSHOT SAVED]", snap)
    except Exception as e:
        print("[SNAPSHOT SAVE ERROR]", e)


def fetch_market_data_loop():
    """
    Background loop running inside Render:
    - uses CURRENT_ACCESS_TOKEN
    - pulls market data from Zerodha
    - updates CACHE
    - writes snapshot to disk
    """
    global CURRENT_ACCESS_TOKEN, CACHE
    print("[THREAD] fetch_market_data_loop started")

    while True:
        try:
            if not CURRENT_ACCESS_TOKEN:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            kite_local = KiteConnect(api_key=KITE_API_KEY)
            kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

            # Get quotes for indices
            # NOTE: BANKNIFTY sometimes needs "NSE:BANKNIFTY" or "NSE:NIFTY BANK"
            # We'll try NIFTY first; if BANKNIFTY fails we'll just skip it.
            data_to_fetch = ["NSE:NIFTY 50", "NSE:BANKNIFTY"]
            try:
                quote_data = kite_local.quote(data_to_fetch)
            except Exception as inner_e:
                # try fallback alt symbol for banknifty
                print("[QUOTE ERROR primary]", inner_e)
                try:
                    quote_data = kite_local.quote(["NSE:NIFTY 50", "NSE:NIFTY BANK"])
                except Exception as inner_e2:
                    print("[QUOTE ERROR fallback]", inner_e2)
                    quote_data = {}

            new_spot = {}
            if "NSE:NIFTY 50" in quote_data:
                new_spot["NIFTY_50"] = quote_data["NSE:NIFTY 50"]["last_price"]

            # banknifty might come from either key
            if "NSE:BANKNIFTY" in quote_data:
                new_spot["BANKNIFTY"] = quote_data["NSE:BANKNIFTY"]["last_price"]
            elif "NSE:NIFTY BANK" in quote_data:
                new_spot["BANKNIFTY"] = quote_data["NSE:NIFTY BANK"]["last_price"]

            if new_spot:
                CACHE["spot"] = new_spot
                CACHE["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                CACHE["stale"] = False  # fresh pull
                print("[FETCH OK]", CACHE["cached_at"], CACHE["spot"])

                # save snapshot so we have fallback on reboot/weekend
                save_snapshot_to_disk()

            else:
                print("[FETCH WARN] got no new_spot from quote()")

        except Exception as e:
            print("[FETCH ERROR]", str(e))

        time.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
def on_startup():
    global CURRENT_ACCESS_TOKEN
    # load last token from disk (if exists)
    if os.path.exists(ACCESS_FILE):
        try:
            with open(ACCESS_FILE, "r") as f:
                CURRENT_ACCESS_TOKEN = f.read().strip()
            print("[TOKEN LOADED FROM DISK]", CURRENT_ACCESS_TOKEN[:6] + "******")
        except Exception as e:
            print("[TOKEN LOAD ERROR]", e)
    # load snapshot for weekend / cold boot
    load_snapshot_from_disk()
    # start background fetch thread
    t = threading.Thread(target=fetch_market_data_loop, daemon=True)
    t.start()


@app.get("/")
def home():
    return {
        "message": "HedgeAI backend live",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": CACHE["stale"],
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": CACHE["stale"],
    }


@app.get("/latest")
def latest():
    # Always answer with CACHE + lot_sizes so frontend can build UI
    return {
        "status": "ok",
        "data": CACHE,
    }


@app.post("/admin/set_token")
async def set_token(request: Request, body: TokenBody):
    """
    You call this manually with curl each morning to update Zerodha token:
    curl -X POST "https://hedge-ai.onrender.com/admin/set_token" \
      -H "Content-Type: application/json" \
      -H "X-ADMIN-KEY: HedgeAI_Admin_2025" \
      -d '{"access_token":"YOUR_TOKEN"}'
    """
    global CURRENT_ACCESS_TOKEN
    admin_header = request.headers.get("X-ADMIN-KEY", "")

    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")

    token = body.access_token.strip()
    if len(token) < 5:
        raise HTTPException(status_code=400, detail="Bad token")

    CURRENT_ACCESS_TOKEN = token
    print("[TOKEN UPDATED VIA ADMIN]", CURRENT_ACCESS_TOKEN[:6] + "******")

    # Save to disk so it persists across deploy / restart
    try:
        with open(ACCESS_FILE, "w") as f:
            f.write(CURRENT_ACCESS_TOKEN)
        print("[TOKEN SAVED TO DISK]")
    except Exception as e:
        print("[TOKEN SAVE ERROR]", e)

    return {"status": "ok", "message": "token saved and fetch started"}


@app.get("/option_quote")
def option_quote(
    instrument: str,
    expiry: str,
    strike: float,
    opt_type: str,
):
    """
    Placeholder for now.
    Frontend calls this to fill Premium & IV box.

    We'll later map instrument+expiry+strike+type -> Zerodha instrument token,
    call kite.quote() on that single option contract,
    and return last_price + implied volatility.

    For now return dummies so UI shows numbers instead of '—'.
    """
    try:
        strike_val = float(strike)
    except:
        strike_val = None

    dummy_premium = 120.5
    dummy_iv = 14.2

    return {
        "status": "ok",
        "instrument": instrument,
        "expiry": expiry,
        "strike": strike_val,
        "type": opt_type,
        "premium": dummy_premium,
        "iv": dummy_iv,
    }
