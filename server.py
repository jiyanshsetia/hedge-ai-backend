import os
import time
import threading
import json
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Allow all origins (for Shopify embed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can restrict to your domain later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from pydantic import BaseModel
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY", "HedgeAI_Admin_2025")
KITE_API_KEY = os.getenv("KITE_API_KEY", "0r1dt27vy4vqg86q")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "3p5f50cd717o35vo4t5cto2714fpn1us")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# this must be updated via /admin/set_token or snapshot restore
CURRENT_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")

# cache for spot + snapshot
CACHE = {
    "cached_at": None,
    "spot": None,
    "chain": None,
    "stale": True,
    "lot_sizes": {
        "NIFTY_50": 75,
        "BANKNIFTY": 35,
    }
}

SNAPSHOT_FILE = "snapshot.json"

app = FastAPI()

class TokenBody(BaseModel):
    access_token: str

def get_kite_client():
    """Return KiteConnect client with active token."""
    if not CURRENT_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="No access token loaded")
    kite_local = KiteConnect(api_key=KITE_API_KEY)
    kite_local.set_access_token(CURRENT_ACCESS_TOKEN)
    return kite_local

# ---------------------------------
# helper: save snapshot to disk (Render can reload after sleep)
# ---------------------------------
def save_snapshot():
    snap = {
        "cached_at": CACHE["cached_at"],
        "spot": CACHE["spot"],
        "lot_sizes": CACHE["lot_sizes"],
    }
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
        print("[SNAPSHOT SAVED]")
    except Exception as e:
        print("[SNAPSHOT SAVE ERR]", e)

def load_snapshot():
    global CACHE
    if not os.path.exists(SNAPSHOT_FILE):
        print("[SNAPSHOT] no snapshot file yet")
        return
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snap = json.load(f)
        CACHE["cached_at"] = snap.get("cached_at")
        CACHE["spot"] = snap.get("spot")
        CACHE["lot_sizes"] = snap.get("lot_sizes", CACHE["lot_sizes"])
        CACHE["stale"] = True
        print("[SNAPSHOT LOADED]", CACHE["cached_at"], CACHE["spot"])
    except Exception as e:
        print("[SNAPSHOT LOAD ERR]", e)

load_snapshot()

# ---------------------------------
# background fetcher: keeps /latest fresh while market is open
# ---------------------------------
def fetch_market_data_loop():
    global CACHE
    while True:
        try:
            if not CURRENT_ACCESS_TOKEN:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            kite_local = KiteConnect(api_key=KITE_API_KEY)
            kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

            # get NIFTY_50 spot (NSE index quote)
            q = kite_local.quote(["NSE:NIFTY 50"])
            spot_price = q["NSE:NIFTY 50"]["last_price"]

            CACHE["cached_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            CACHE["spot"] = {
                "NIFTY_50": spot_price
            }
            CACHE["chain"] = {
                "note": "option greeks / IV coming soon"
            }
            CACHE["stale"] = False

            save_snapshot()
            print("[FETCH OK]", CACHE["cached_at"], CACHE["spot"])

        except Exception as e:
            print("[FETCH ERROR]", str(e))
            # don't kill the loop
        time.sleep(POLL_INTERVAL_SECONDS)

t = threading.Thread(target=fetch_market_data_loop, daemon=True)
t.start()
print("[THREAD] fetch_market_data_loop started")

# ---------------------------------
# HEALTH + CORE ENDPOINTS
# ---------------------------------
@app.get("/")
def home():
    return {
        "message": "HedgeAI backend live",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"]
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": CACHE["stale"]
    }

@app.get("/latest")
def latest():
    if CACHE["cached_at"] is None:
        return JSONResponse({"message": "No cached data yet"}, status_code=200)
    return {
        "status": "ok",
        "data": CACHE
    }

@app.post("/admin/set_token")
async def set_token(request: Request, body: TokenBody):
    global CURRENT_ACCESS_TOKEN
    admin_header = request.headers.get("X-ADMIN-KEY", "")
    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")

    if not body.access_token or len(body.access_token) < 5:
        raise HTTPException(status_code=400, detail="Bad token")

    CURRENT_ACCESS_TOKEN = body.access_token

    # persist for Render restart survival
    with open("access_token.json", "w") as f:
        json.dump({"access_token": CURRENT_ACCESS_TOKEN}, f)
    print("[TOKEN SAVED TO DISK]")

    print("[TOKEN UPDATED VIA ADMIN]", CURRENT_ACCESS_TOKEN[:8] + "******")
    return {"status": "ok", "message": "token saved and fetch started"}

# ---------------------------------
# NEW 1: /expiries
# ---------------------------------
@app.get("/expiries")
def get_expiries(instrument: str = Query(..., description="NIFTY_50 or BANKNIFTY")):
    """
    Return next ~4 expiries for the given index instrument using kite.instruments().
    We'll filter F&O contracts on that index and sort unique expiry dates.
    """
    try:
        kite_local = get_kite_client()
        instruments = kite_local.instruments("NFO")  # derivatives segment
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"instruments() failed: {e}")

    # map ui name -> tradingsymbol prefix in Zerodha
    # NIFTY_50 -> 'NIFTY'; BANKNIFTY -> 'BANKNIFTY'
    prefix_map = {
        "NIFTY_50": "NIFTY",
        "BANKNIFTY": "BANKNIFTY"
    }
    if instrument not in prefix_map:
        raise HTTPException(status_code=400, detail="Unsupported instrument")

    prefix = prefix_map[instrument]

    expiries = []
    for item in instruments:
        # item example keys: tradingsymbol, name, instrument_type, segment, exchange, strike, expiry, etc.
        if item.get("name") == prefix and item.get("instrument_type") in ("CE", "PE"):
            exp = item.get("expiry")
            if exp:
                expiries.append(exp)

    # unique + sort soonest first
    expiries = sorted(list({e for e in expiries}))[:4]

    # format for frontend: label "28 Oct 2025", value "2025-10-28"
    out = []
    for d in expiries:
        # d is datetime.date or datetime
        if isinstance(d, datetime):
            dd = d.date()
        else:
            dd = d
        label = dd.strftime("%d %b %Y")
        value = dd.strftime("%Y-%m-%d")
        out.append({"label": label, "value": value})

    return {"instrument": instrument, "expiries": out}

# ---------------------------------
# helper to build Zerodha option symbol
# ---------------------------------
def build_option_symbol(instrument: str, expiry_yyyy_mm_dd: str, strike: float, opt_type: str):
    """
    instrument: 'NIFTY_50' or 'BANKNIFTY'
    expiry_yyyy_mm_dd: '2025-10-30'
    strike: 25650
    opt_type: 'CE' or 'PE'
    Zerodha format: 'NFO:NIFTY25OCT25650CE'
    """

    prefix_map = {
        "NIFTY_50": "NIFTY",
        "BANKNIFTY": "BANKNIFTY"
    }
    if instrument not in prefix_map:
        raise HTTPException(status_code=400, detail="Unsupported instrument")
    base = prefix_map[instrument]

    # convert '2025-10-30' -> '25OCT' style
    dt = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d").date()
    expiry_code = dt.strftime("%y").upper() + dt.strftime("%b").upper()  # '25' + 'OCT' -> '25OCT'

    # strike must not have decimal for index options
    strike_int = int(round(float(strike)))

    return f"NFO:{base}{expiry_code}{strike_int}{opt_type}"

# ---------------------------------
# NEW 2: /option_quote
# ---------------------------------
@app.get("/option_quote")
def option_quote(
    instrument: str = Query(..., description="NIFTY_50 or BANKNIFTY"),
    expiry: str = Query(..., description="YYYY-MM-DD"),
    strike: float = Query(..., description="e.g. 25650"),
    opt_type: str = Query(..., description="CE or PE")
):
    """
    Return latest option premium + IV for a specific strike.
    """
    sym = build_option_symbol(instrument, expiry, strike, opt_type)

    try:
        kite_local = get_kite_client()
        q = kite_local.quote([sym])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"quote() failed: {e}")

    if sym not in q:
        raise HTTPException(status_code=404, detail="No quote for that contract")

    data = q[sym]

    last_price = data.get("last_price")
    iv = data.get("implied_volatility")

    return {
        "symbol": sym,
        "premium": last_price,
        "iv": iv
    }
