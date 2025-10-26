import time
import json
import threading
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from kiteconnect import KiteConnect

########################################
# HARD-CODED CONFIG
########################################

ADMIN_KEY = "HedgeAI_Admin_2025"

# Zerodha Kite app credentials
KITE_API_KEY = "0r1dt27vy4vqg86q"
KITE_API_SECRET = "3p5f50cd717o35vo4t5cto2714fpn1us"

# poll interval for live index snapshot (sec)
POLL_INTERVAL_SECONDS = 60

# index mapping and lot sizes (updated BANKNIFTY lot = 35)
INSTRUMENTS = {
    "NIFTY_50": {
        "kite_symbol": "NSE:NIFTY 50",
        "lot_size": 75
    },
    "BANKNIFTY": {
        "kite_symbol": "NSE:BANKNIFTY",
        "lot_size": 35
    },
    # can add FINNIFTY etc later
}

SNAPSHOT_FILE = "last_snapshot.json"

########################################
# GLOBAL STATE
########################################

app = FastAPI()

CURRENT_ACCESS_TOKEN: Optional[str] = None

CACHE = {
    "cached_at": None,
    "spot": {},       # {"NIFTY_50": 25795.15, "BANKNIFTY": 43000.0}
    "chain": {},
    "stale": True
}

########################################
# HELPERS
########################################

def load_snapshot_from_disk():
    global CACHE
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            data = json.load(f)
        CACHE["cached_at"] = data.get("cached_at")
        CACHE["spot"] = data.get("spot", {})
        CACHE["chain"] = data.get("chain", {})
        CACHE["stale"] = True  # snapshot = stale
        print("[SNAPSHOT LOADED]", CACHE["cached_at"], CACHE["spot"])
    except FileNotFoundError:
        print("[SNAPSHOT] no snapshot file yet")
    except Exception as e:
        print("[SNAPSHOT ERROR]", str(e))

def save_snapshot_to_disk():
    try:
        snap = {
            "cached_at": CACHE["cached_at"],
            "spot": CACHE["spot"],
            "chain": CACHE["chain"],
        }
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f, indent=2)
        print("[SNAPSHOT SAVED]", CACHE["cached_at"], CACHE["spot"])
    except Exception as e:
        print("[SNAPSHOT SAVE ERROR]", str(e))

def get_kite():
    """Return a KiteConnect instance with the current access token set."""
    if not CURRENT_ACCESS_TOKEN:
        raise Exception("No access token set yet")
    kite_local = KiteConnect(api_key=KITE_API_KEY)
    kite_local.set_access_token(CURRENT_ACCESS_TOKEN)
    return kite_local

def fetch_once():
    """
    Pull quotes (NIFTY, BANKNIFTY spot).
    Update CACHE with latest prices.
    Save snapshot.
    """
    global CACHE
    if not CURRENT_ACCESS_TOKEN:
        # can't fetch yet
        return

    try:
        kite_local = get_kite()

        symbols = [cfg["kite_symbol"] for cfg in INSTRUMENTS.values()]
        quote_data = kite_local.quote(symbols)

        spot_result = {}
        for name, cfg in INSTRUMENTS.items():
            sym = cfg["kite_symbol"]
            if sym in quote_data:
                spot_result[name] = quote_data[sym]["last_price"]

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        CACHE["cached_at"] = now_str
        CACHE["spot"] = spot_result
        CACHE["chain"] = {
            "note": "option greeks / IV coming soon"
        }
        CACHE["stale"] = False

        print("[FETCH OK]", now_str, spot_result)
        save_snapshot_to_disk()

    except Exception as e:
        print("[FETCH ERROR]", str(e))
        # keep old CACHE for weekend / downtime

def fetch_market_data_loop():
    print("[THREAD] fetch_market_data_loop started")
    while True:
        fetch_once()
        time.sleep(POLL_INTERVAL_SECONDS)

########################################
# MODELS
########################################

class TokenBody(BaseModel):
    access_token: str

########################################
# ROUTES: BASIC
########################################

@app.on_event("startup")
def on_startup():
    load_snapshot_from_disk()
    t = threading.Thread(target=fetch_market_data_loop, daemon=True)
    t.start()

@app.get("/")
def home():
    return {
        "message": "HedgeAI backend running",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"]
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": CACHE.get("stale", True),
    }

@app.get("/latest")
def latest():
    # if we never fetched after boot, try loading snapshot again
    if CACHE["cached_at"] is None and not CACHE["spot"]:
        load_snapshot_from_disk()

    if CACHE["cached_at"] is None and not CACHE["spot"]:
        return JSONResponse({"message": "No cached data yet"}, status_code=200)

    return {
        "status": "ok",
        "data": {
            "cached_at": CACHE["cached_at"],
            "spot": CACHE["spot"],
            "chain": CACHE["chain"],
            "stale": CACHE.get("stale", True),
            "lot_sizes": {name: cfg["lot_size"] for name, cfg in INSTRUMENTS.items()}
        }
    }

########################################
# ROUTE: ADMIN SET TOKEN
########################################

@app.post("/admin/set_token")
async def set_token(request: Request, body: TokenBody):
    """
    You call this with curl:
    curl -X POST "http://127.0.0.1:8000/admin/set_token" \
      -H "Content-Type: application/json" \
      -H "X-ADMIN-KEY: HedgeAI_Admin_2025" \
      -d '{"access_token":"<ACCESS_TOKEN_HERE>"}'
    """
    global CURRENT_ACCESS_TOKEN

    admin_header = request.headers.get("X-ADMIN-KEY", "")
    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")

    if not body.access_token or len(body.access_token) < 5:
        raise HTTPException(status_code=422, detail="Bad token format")

    CURRENT_ACCESS_TOKEN = body.access_token
    print("[TOKEN UPDATED VIA ADMIN]", CURRENT_ACCESS_TOKEN[:6] + "******")

    try:
        with open("current_access_token.txt", "w") as f:
            f.write(CURRENT_ACCESS_TOKEN)
        print("[TOKEN SAVED TO DISK]")
    except Exception as e:
        print("[TOKEN SAVE ERROR]", str(e))

    # attempt immediate fetch so CACHE is warm
    fetch_once()

    return {"status": "ok", "message": "token saved and fetch started"}

########################################
# ROUTE: OPTION QUOTE (PREMIUM + IV)
########################################

@app.get("/option_quote")
def option_quote(
    instrument: str = Query(..., description="e.g. NIFTY_50 or BANKNIFTY"),
    expiry: str = Query(..., description="e.g. 28 Oct 2025"),
    strike: float = Query(..., description="e.g. 25800"),
    opt_type: str = Query(..., description="'CALL' or 'PUT'")
):
    """
    Returns premium (LTP) and IV for a specific option contract.
    Frontend will call this when user selects expiry/strike/type.

    We have to map (instrument, expiry, strike, CALL/PUT)
    to the trading symbol Zerodha expects.

    NOTE:
    - Actual instrument_token / tradingsymbol naming for Zerodha index options looks like:
      NIFTY25OCT25800CE or BANKNIFTY25OCT25800CE etc.
    - This is slightly tricky because it's YYMMMDD / YYMON... style.
    - We'll TEMPORARILY return dummy values so frontend can wire.
    - Next step we'll generate correct tradingsymbol from expiry.
    """

    # TEMP MOCK RESPONSE so UI works:
    # after wiring frontend, we'll replace this with real kite.quote([tradingsymbol])
    dummy_premium = 120.50
    dummy_iv = 14.2

    return {
        "status": "ok",
        "instrument": instrument,
        "expiry": expiry,
        "strike": strike,
        "type": opt_type,
        "premium": dummy_premium,
        "iv": dummy_iv
    }