import os
import time
import threading
import json
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# ----------------- ENV / CONFIG -----------------
load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY", "HedgeAI_Admin_2025")
KITE_API_KEY = os.getenv("KITE_API_KEY", "0r1dt27vy4vqg86q")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "3p5f50cd717o35vo4t5cto2714fpn1us")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# access token will be injected via /admin/set_token after deploy
CURRENT_ACCESS_TOKEN = ""  # Render memory reset = we repush token

CACHE = {
    "cached_at": None,
    "spot": None,
    "chain": None,
    "lot_sizes": {
        "NIFTY_50": 75,
        "BANKNIFTY": 35,
    },
}

SNAPSHOT_FILE = "snapshot.json"

def load_snapshot():
    global CACHE
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE, "r") as f:
                snap = json.load(f)
            CACHE["cached_at"] = snap.get("cached_at")
            CACHE["spot"] = snap.get("spot")
            CACHE["chain"] = snap.get("chain", CACHE["chain"])
            print("[SNAPSHOT LOADED]", CACHE["cached_at"], CACHE["spot"])
        except Exception as e:
            print("[SNAPSHOT ERROR]", str(e))
    else:
        print("[SNAPSHOT] no snapshot file yet")

def save_snapshot():
    snap = {
        "cached_at": CACHE["cached_at"],
        "spot": CACHE["spot"],
        "chain": CACHE["chain"],
    }
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
        print("[SNAPSHOT SAVED]")
    except Exception as e:
        print("[SNAPSHOT SAVE ERROR]", str(e))


# ----------------- FASTAPI APP + CORS -----------------
app = FastAPI()

# CORS for Shopify frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # allow any origin (Shopify storefront)
    allow_credentials=True,
    allow_methods=["*"],          # GET, POST, OPTIONS, etc.
    allow_headers=["*"],
)

# Make sure OPTIONS never returns 405
@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    return PlainTextResponse("ok", status_code=200)


kite = KiteConnect(api_key=KITE_API_KEY)

class TokenBody(BaseModel):
    access_token: str


# ----------------- BACKGROUND FETCH LOOP -----------------
def fetch_market_data_loop():
    """
    Poll Zerodha using CURRENT_ACCESS_TOKEN.
    Update CACHE["spot"] etc.
    If token is invalid/expired or market closed -> log error, keep old snapshot.
    """
    global CURRENT_ACCESS_TOKEN, CACHE

    while True:
        try:
            if not CURRENT_ACCESS_TOKEN:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            kite_local = KiteConnect(api_key=KITE_API_KEY)
            kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

            quotes = kite_local.quote(["NSE:NIFTY 50", "NSE:BANKNIFTY"])

            nifty_spot = quotes.get("NSE:NIFTY 50", {}).get("last_price")
            bank_spot  = quotes.get("NSE:BANKNIFTY", {}).get("last_price")

            CACHE["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            CACHE["spot"] = {}
            if nifty_spot is not None:
                CACHE["spot"]["NIFTY_50"] = nifty_spot
            if bank_spot is not None:
                CACHE["spot"]["BANKNIFTY"] = bank_spot

            if CACHE.get("chain") is None:
                CACHE["chain"] = {"note": "option greeks coming soon"}

            print("[FETCH OK]", CACHE["cached_at"], CACHE["spot"])
            save_snapshot()

        except Exception as e:
            print("[FETCH ERROR]", str(e))

        time.sleep(POLL_INTERVAL_SECONDS)


thread_started = False
def ensure_thread():
    global thread_started
    if not thread_started:
        t = threading.Thread(target=fetch_market_data_loop, daemon=True)
        t.start()
        thread_started = True
        print("[THREAD] fetch_market_data_loop started")


@app.on_event("startup")
async def on_startup():
    load_snapshot()
    ensure_thread()


# ----------------- ROUTES (PUBLIC) -----------------
@app.get("/")
def home():
    return {
        "message": "HedgeAI backend running",
        "has_token": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
    }

@app.get("/health")
def health():
    stale = True
    if CACHE["cached_at"]:
        stale = False
    return {
        "status": "ok",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": stale,
    }

@app.get("/latest")
def latest():
    stale = True
    if CACHE["cached_at"]:
        stale = False
    return {
        "status": "ok",
        "data": {
            "cached_at": CACHE["cached_at"],
            "spot": CACHE["spot"],
            "chain": CACHE["chain"],
            "stale": stale,
            "lot_sizes": CACHE["lot_sizes"],
        }
    }

@app.get("/expiries")
def expiries(instrument: str):
    """
    Next 4 expiries for dropdown.
    If Zerodha call fails, we return fallback so Shopify UI still works.
    """
    global CURRENT_ACCESS_TOKEN

    fallback = [
        {"label": "28 Oct 2025", "value": "2025-10-28"},
        {"label": "04 Nov 2025", "value": "2025-11-04"},
        {"label": "11 Nov 2025", "value": "2025-11-11"},
        {"label": "18 Nov 2025", "value": "2025-11-18"},
    ]

    # no token in Render memory yet? -> fallback
    if not CURRENT_ACCESS_TOKEN:
        return {"instrument": instrument, "expiries": fallback, "note": "no token / fallback"}

    try:
        kite_local = KiteConnect(api_key=KITE_API_KEY)
        kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

        all_instr = kite_local.instruments()

        # map HedgeAI instrument -> Zerodha symbol prefix
        base_symbol = "NIFTY"
        if instrument == "BANKNIFTY":
            base_symbol = "BANKNIFTY"

        seen = set()
        expiries_list = []

        for row in all_instr:
            if row.get("segment") != "NFO-OPT":
                continue
            tsym = row.get("tradingsymbol", "")
            if not tsym.startswith(base_symbol):
                continue

            exp_date = row.get("expiry")
            if not exp_date:
                continue

            if isinstance(exp_date, str):
                dt_val = exp_date
                lbl_val = exp_date
            else:
                # datetime.date or datetime
                dt_val = exp_date.strftime("%Y-%m-%d")
                lbl_val = exp_date.strftime("%d %b %Y")

            if dt_val not in seen:
                seen.add(dt_val)
                expiries_list.append({
                    "label": lbl_val,
                    "value": dt_val
                })

        expiries_list.sort(key=lambda x: x["value"])
        expiries_list = expiries_list[:4]

        if not expiries_list:
            expiries_list = fallback

        return {
            "instrument": instrument,
            "expiries": expiries_list
        }

    except Exception as e:
        print("[EXPIRIES ERROR]", str(e))
        return {
            "instrument": instrument,
            "expiries": fallback,
            "note": "fallback (error/market closed)"
        }


from fastapi import Query

# helper: convert "28 Oct 2025" -> Zerodha style expiry like "2025-10-28"
def parse_expiry_label_to_yyyy_mm_dd(expiry_label: str):
    # expiry_label will usually be "28 Oct 2025" from frontend
    import datetime
    try:
        d = datetime.datetime.strptime(expiry_label, "%d %b %Y")
        return d.strftime("%Y-%m-%d")
    except:
        # maybe frontend already sent "2025-10-28"
        return expiry_label

# helper: build tradingsymbol for NIFTY options, e.g. "NIFTY25O2825650CE"
# NOTE: Zerodha naming is like: INDEX + YY + MLETTER + D + STRIKE + CE/PE
# We will keep this simple and approximate.
def build_option_symbol_nifty(expiry_yyyy_mm_dd: str, strike: int, opttype: str):
    # opttype: "CE" or "PE"
    # expiry_yyyy_mm_dd: "2025-10-28"
    import datetime
    dt = datetime.datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d")

    # Zerodha weekly format is like NIFTY25O2825650CE:
    #  - "NIFTY"
    #  - YY last 2 digits
    #  - month as single capital letter (Jan=A, Feb=B, ... Oct=O, Nov=P, Dec=X)
    #  - day (no leading zero)
    #  - strike
    #  - CE/PE
    #
    # We'll map month -> letter like NSE convention for weekly index options.
    month_code_map = {
        1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",7:"G",8:"H",9:"I",10:"O",11:"P",12:"X"
    }
    yy = str(dt.year)[2:]
    mcode = month_code_map[dt.month]
    day_no_leading_zero = str(dt.day)
    tsym = f"NIFTY{yy}{mcode}{day_no_leading_zero}{int(strike)}{opttype.upper()}"
    return tsym

@app.get("/quote")
def get_option_quote(
    instrument: str = Query(..., description="e.g. NIFTY_50"),
    expiry: str = Query(..., description="e.g. '28 Oct 2025' or '2025-10-28'"),
    strike: int = Query(..., description="e.g. 25800"),
    type: str = Query(..., description="'CE' or 'PE'")
):
    """
    Return REAL market data for a single option contract.
    This is the ONLY source of option price for the frontend.
    """
    global CURRENT_ACCESS_TOKEN

    if not CURRENT_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="No access token loaded")

    # only supporting NIFTY right now
    if instrument.upper() not in ["NIFTY", "NIFTY_50", "NIFTY50", "NIFTY 50"]:
        raise HTTPException(status_code=400, detail="instrument not supported yet")

    expiry_std = parse_expiry_label_to_yyyy_mm_dd(expiry)

    try:
        kite_local = KiteConnect(api_key=KITE_API_KEY)
        kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

        tradingsymbol = build_option_symbol_nifty(expiry_std, strike, type)
        # Zerodha index options for NIFTY are on NSE index option segment "NFO"
        # So instrument_token is "NFO:"+tradingsymbol
        full_symbol = f"NFO:{tradingsymbol}"

        q = kite_local.quote([full_symbol])
        # q looks like { "NFO:NIFTY25O2825650CE": { "last_price": 191.2, ... }}

        data = q.get(full_symbol)
        if not data:
            raise Exception("No data for that contract")

        option_price = data.get("last_price")
        iv = data.get("implied_volatility")  # sometimes provided
        # Zerodha quote() doesn't directly spit greeks for index options
        # We'll send null for now. Frontend can estimate if it wants.
        delta = None
        theta = None
        vega  = None

        # you already know your lot sizes:
        lot_size_map = {
            "NIFTY": 75,
            "NIFTY_50": 75,
            "NIFTY 50": 75,
            "NIFTY50": 75
        }
        lotSize = lot_size_map.get(instrument.upper(), 75)

        # expiry_days to help theta scaling:
        import datetime
        today = datetime.datetime.utcnow().date()
        expd  = datetime.datetime.strptime(expiry_std, "%Y-%m-%d").date()
        expiry_days = max((expd - today).days, 0)

        return {
            "option_price": option_price,
            "iv": iv,
            "delta": delta,
            "theta": theta,
            "vega": vega,
            "lotSize": lotSize,
            "expiry_days": expiry_days,
            "tradingsymbol": tradingsymbol,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# ----------------- ROUTES (ADMIN) -----------------
@app.post("/admin/set_token")
async def set_token(request: Request, body: TokenBody):
    """
    Admin route to inject Zerodha access_token (the one you generate locally).
    Headers:
      X-ADMIN-KEY: HedgeAI_Admin_2025
    Body:
      { "access_token": "<token>" }
    """
    global CURRENT_ACCESS_TOKEN

    admin_header = request.headers.get("X-ADMIN-KEY", "")
    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")

    if not body.access_token or len(body.access_token) < 5:
        raise HTTPException(status_code=400, detail="Bad token")

    CURRENT_ACCESS_TOKEN = body.access_token
    print("[TOKEN UPDATED VIA ADMIN]", CURRENT_ACCESS_TOKEN[:10] + "******")

    # also drop to disk so if Render restarts in same container it can reload (not guaranteed on free tier)
    with open("access_token.json", "w") as f:
        json.dump({"access_token": CURRENT_ACCESS_TOKEN}, f)

    return {"status": "ok", "message": "token saved and fetch started"}
