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

# --- ENV / CONFIG ---
load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY", "HedgeAI_Admin_2025")
KITE_API_KEY = os.getenv("KITE_API_KEY", "0r1dt27vy4vqg86q")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "3p5f50cd717o35vo4t5cto2714fpn1us")

# This will get updated via /admin/set_token
CURRENT_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# --- CACHE / STATE ---
CACHE = {
    "cached_at": None,
    "spot": None,
    "chain": None,
    "lot_sizes": {
        "NIFTY_50": 75,
        "BANKNIFTY": 35
    },
}

SNAPSHOT_FILE = "snapshot.json"

def load_snapshot():
    global CACHE
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE, "r") as f:
                snap = json.load(f)
            # minimal sanity
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

# --- FASTAPI APP + CORS ---
app = FastAPI()

# Allow Shopify to call us from frontend. Also handle preflight OPTIONS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # later you can restrict to your domain
    allow_credentials=True,
    allow_methods=["*"],          # includes OPTIONS, GET, POST etc.
    allow_headers=["*"],
)

# Also explicitly handle OPTIONS root-level (some infra hits OPTIONS /latest etc.)
@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    # FastAPI+CORSMiddleware should already set headers,
    # but we reply 200 here so Render doesn't do 405.
    return PlainTextResponse("ok", status_code=200)

kite = KiteConnect(api_key=KITE_API_KEY)

class TokenBody(BaseModel):
    access_token: str

# --- BACKGROUND FETCH LOOP ---
def fetch_market_data_loop():
    """
    Keeps polling Zerodha to refresh CACHE with spot etc.
    If token is invalid or market is closed, we just log and continue.
    """
    global CURRENT_ACCESS_TOKEN, CACHE

    while True:
        try:
            if not CURRENT_ACCESS_TOKEN:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            kite_local = KiteConnect(api_key=KITE_API_KEY)
            kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

            # pull spot for NIFTY 50 and BankNifty
            quotes = kite_local.quote(["NSE:NIFTY 50", "NSE:BANKNIFTY"])
            nifty_spot = quotes.get("NSE:NIFTY 50", {}).get("last_price")
            bn_spot    = quotes.get("NSE:BANKNIFTY", {}).get("last_price")

            CACHE["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            CACHE["spot"] = {}
            if nifty_spot is not None:
                CACHE["spot"]["NIFTY_50"] = nifty_spot
            if bn_spot is not None:
                CACHE["spot"]["BANKNIFTY"] = bn_spot

            # TODO: chain/greeks
            CACHE["chain"] = CACHE.get("chain") or {"note": "option greeks coming soon"}

            print("[FETCH OK]", CACHE["cached_at"], CACHE["spot"])
            save_snapshot()

        except Exception as e:
            print("[FETCH ERROR]", str(e))

        time.sleep(POLL_INTERVAL_SECONDS)

# Kick off loop in a background thread when app starts
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

# --- ROUTES ---

@app.get("/")
def home():
    return {
        "message": "HedgeAI backend running",
        "has_token": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
    }

@app.get("/health")
def health():
    # stale if > 5 mins old or never set
    stale = True
    if CACHE["cached_at"]:
        # we won't calculate minutes exactly here, just say False if we have data
        stale = False
    return {
        "status": "ok",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": stale,
    }

@app.get("/latest")
def latest():
    # we always return something so frontend never dies
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
    Returns next 4 expiries for dropdown. If Zerodha call fails,
    we still return placeholders (so Shopify UI won't hang).
    """
    global CURRENT_ACCESS_TOKEN

    # default fallback expiries (Tue-Tue-Tue style)
    fallback = [
        {"label": "28 Oct 2025", "value": "2025-10-28"},
        {"label": "04 Nov 2025", "value": "2025-11-04"},
        {"label": "11 Nov 2025", "value": "2025-11-11"},
        {"label": "18 Nov 2025", "value": "2025-11-18"},
    ]

    if not CURRENT_ACCESS_TOKEN:
        # no token: just return fallback so frontend doesn't break
        return {"instrument": instrument, "expiries": fallback}

    try:
        kite_local = KiteConnect(api_key=KITE_API_KEY)
        kite_local.set_access_token(CURRENT_ACCESS_TOKEN)

        # Get all instruments from Zerodha
        all_instr = kite_local.instruments()
        # Filter only index options of this instrument
        # For NIFTY_50 use tradingsymbol like "NIFTY", "BANKNIFTY" etc.
        base_symbol = "NIFTY"
        if instrument == "BANKNIFTY":
            base_symbol = "BANKNIFTY"

        # pick only options of that symbol, collect (expiry)
        seen = set()
        expiries_list = []
        for row in all_instr:
            if row.get("segment") != "NFO-OPT":
                continue
            if not row.get("tradingsymbol","").startswith(base_symbol):
                continue
            exp_date = row.get("expiry")
            if not exp_date:
                continue
            # convert date obj -> label/value
            if isinstance(exp_date, str):
                # sometimes it might already be string
                dt = exp_date
                lbl = exp_date
            else:
                # assume datetime.date or datetime.datetime
                dt = exp_date.strftime("%Y-%m-%d")
                lbl = exp_date.strftime("%d %b %Y")
            if dt not in seen:
                seen.add(dt)
                expiries_list.append({"label": lbl, "value": dt})

        # sort by date value
        def sort_key(x):
            return x["value"]
        expiries_list.sort(key=sort_key)

        # take first 4
        expiries_list = expiries_list[:4]

        # safety: if empty, fallback
        if not expiries_list:
            expiries_list = fallback

        return {
            "instrument": instrument,
            "expiries": expiries_list
        }

    except Exception as e:
        print("[EXPIRIES ERROR]", str(e))
        # return fallback instead of 500
        return {
            "instrument": instrument,
            "expiries": fallback,
            "note": "fallback (error/market closed)"
        }
