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


@app.get("/option_quote")
def option_quote(instrument: str, expiry: str, strike: str, opt_type: str):
    """
    Return premium + IV for a specific option contract:
    - instrument: "NIFTY_50" or "BANKNIFTY"
    - expiry: "2025-10-28" (YYYY-MM-DD)
    - strike: "25800" (string/number)
    - opt_type: "CE" or "PE"
    """

    global CURRENT_ACCESS_TOKEN

    # safe fallback so frontend doesn't die
    fallback = {
        "premium": None,
        "iv": None,
        "lot_size": CACHE["lot_sizes"].get(instrument, 75),
        "note": "fallback/no token or not found"
    }

    if not CURRENT_ACCESS_TOKEN:
        return fallback

    try:
        kite_local = KiteConnect(api_key=KITE_API_KEY)
        kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

        # 1. load instrument dump once per request
        all_instr = kite_local.instruments()

        # map "NIFTY_50" -> "NIFTY" underlying in Zerodha
        if instrument == "NIFTY_50":
            underlying_symbol = "NIFTY"
        elif instrument == "BANKNIFTY":
            underlying_symbol = "BANKNIFTY"
        else:
            underlying_symbol = "NIFTY"  # default

        # normalize strike to numeric so we can match exactly
        try:
            strike_val = float(strike)
        except:
            # if somehow we get "25800.0" vs "25800", we'll try float compare later
            strike_val = float(strike.replace(",", ""))

        # normalize expiry -> date object string 'YYYY-MM-DD'
        target_exp = expiry.strip()  # we already get "2025-10-28" style

        # 2. find the row in instruments() that matches all of these:
        #    - NFO-OPT
        #    - name == underlying_symbol
        #    - instrument_type == opt_type (CE/PE)
        #    - strike == strike_val
        #    - expiry matches target_exp
        match_row = None
        for row in all_instr:
            if row.get("segment") != "NFO-OPT":
                continue

            # Zerodha uses "name" or sometimes "tradingsymbol" prefix to indicate underlying
            # We'll check both.
            if row.get("name") != underlying_symbol and not str(row.get("tradingsymbol","")).startswith(underlying_symbol):
                continue

            if row.get("instrument_type") != opt_type:
                continue

            # strike_price is float, compare
            if float(row.get("strike", 0.0)) != strike_val:
                continue

            # expiry compare:
            exp_val = row.get("expiry")
            # expiry can be datetime.date/datetime or string
            if hasattr(exp_val, "strftime"):
                exp_fmt = exp_val.strftime("%Y-%m-%d")
            else:
                exp_fmt = str(exp_val)

            if exp_fmt != target_exp:
                continue

            # this is our contract
            match_row = row
            break

        if not match_row:
            # couldn't match this exact contract, return safe fallback
            return fallback

        token = match_row.get("instrument_token")
        if not token:
            return fallback

        # 3. now we ask quote() for this instrument_token
        quote_key = token  # for index, we send "NSE:XYZ", but for derivatives we send numeric token
        q = kite_local.quote([quote_key])

        qdata = q.get(str(quote_key)) or q.get(quote_key)
        if not qdata:
            return fallback

        ltp = qdata.get("last_price")
        iv_val = qdata.get("implied_volatility")

        return {
            "premium": ltp,
            "iv": iv_val,
            "lot_size": CACHE["lot_sizes"].get(instrument, 75),
            "note": "live"
        }

    except Exception as e:
        print("[OPTION_QUOTE ERROR]", str(e))
        return fallback

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
