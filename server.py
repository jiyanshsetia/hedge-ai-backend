import os
import time
import json
import math
import threading
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY", "HedgeAI_Admin_2025")
KITE_API_KEY = os.getenv("KITE_API_KEY", "0r1dt27vy4vqg86q")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "3p5f50cd717o35vo4t5cto2714fpn1us")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

TOKEN_FILE = "token.json"
SNAPSHOT_FILE = "snapshot.json"

# ------------------ GLOBAL CACHE ------------------
CACHE = {
    "cached_at": None,
    "spot": None,         # {"NIFTY_50": 25977.4, ...}
    "chain": {},          # future: greeks/iv cache
    "lot_sizes": {
        "NIFTY_50": 75,
        "BANKNIFTY": 35
    }
}

CURRENT_ACCESS_TOKEN = None  # will load from token.json

# ------------------ HELPERS ------------------

def load_token_from_disk():
    global CURRENT_ACCESS_TOKEN
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
                CURRENT_ACCESS_TOKEN = data.get("access_token")
                print("[INIT] Loaded token from disk")
        except Exception as e:
            print("[INIT] Failed loading token:", e)

def save_token_to_disk(tok: str):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": tok}, f)
    print("[TOKEN SAVED TO DISK]")

def load_snapshot_from_disk():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE, "r") as f:
                snap = json.load(f)
                CACHE["cached_at"] = snap.get("cached_at")
                CACHE["spot"] = snap.get("spot")
                CACHE["chain"] = snap.get("chain", {})
                print("[INIT] Loaded snapshot from disk", CACHE["cached_at"], CACHE["spot"])
        except Exception as e:
            print("[INIT] Failed loading snapshot:", e)
    else:
        print("[INIT] No snapshot file yet")

def save_snapshot_to_disk():
    blob = {
        "cached_at": CACHE["cached_at"],
        "spot": CACHE["spot"],
        "chain": CACHE["chain"]
    }
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(blob, f)
    print("[SNAPSHOT SAVED]")

def get_kite_client():
    """Return a KiteConnect client with current token set. Raise if missing."""
    if not CURRENT_ACCESS_TOKEN:
        raise Exception("No access token loaded")
    kite_local = KiteConnect(api_key=KITE_API_KEY)
    kite_local.set_access_token(CURRENT_ACCESS_TOKEN)
    return kite_local

def build_nifty_option_symbol(expiry_label: str, strike: int, opt_type: str):
    """
    expiry_label like "28 Oct 2025"
    We must convert to Zerodha tradingsymbol like: NIFTY25O28<strike><CE/PE>
    month code guess:
      Jan=A, Feb=B, Mar=C, Apr=D, May=E, Jun=F, Jul=G, Aug=H, Sep=I, Oct=O, Nov=P, Dec=Z
    We'll map Oct -> O etc.
    """
    # parse date
    dt = datetime.strptime(expiry_label, "%d %b %Y")
    yy = dt.strftime("%y")  # e.g. "25"
    day = dt.strftime("%d") # e.g. "28"
    mon_map = {
        1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",
        7:"G",8:"H",9:"I",10:"O",11:"P",12:"Z"
    }
    mcode = mon_map[dt.month]  # e.g. October -> "O"
    # CE/PE
    tcode = "CE" if opt_type.upper()=="CE" else "PE"
    # final (common retail format Zerodha uses)
    # Example: NIFTY25O2825900CE
    symbol = f"NIFTY{yy}{mcode}{day}{int(strike)}{tcode}"
    return symbol

# ------------------ FASTAPI APP ------------------
app = FastAPI()

# allow Shopify storefront etc
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can tighten later
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TokenBody(BaseModel):
    access_token: str

@app.on_event("startup")
def startup_event():
    global CURRENT_ACCESS_TOKEN
    load_token_from_disk()
    load_snapshot_from_disk()
    # start background fetch thread
    th = threading.Thread(target=fetch_market_data_loop, daemon=True)
    th.start()
    print("[THREAD] fetch_market_data_loop started")

# ------------------ BACKGROUND LOOP ------------------
def fetch_market_data_loop():
    global CURRENT_ACCESS_TOKEN, CACHE
    while True:
        try:
            if not CURRENT_ACCESS_TOKEN:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            kite_local = get_kite_client()

            # fetch spot NIFTY 50
            q = kite_local.quote(["NSE:NIFTY 50"])
            last_price = q["NSE:NIFTY 50"]["last_price"]

            CACHE["cached_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            CACHE["spot"] = {
                "NIFTY_50": last_price
            }

            save_snapshot_to_disk()
            print("[FETCH OK]", CACHE["cached_at"], CACHE["spot"])

        except Exception as e:
            # don't crash thread, just log
            print("[FETCH ERROR]", str(e))

        time.sleep(POLL_INTERVAL_SECONDS)

# ------------------ ROUTES ------------------

@app.get("/")
def home():
    return {
        "message": "HedgeAI backend up",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"]
    }

@app.get("/health")
def health():
    stale_flag = True
    if CACHE["cached_at"]:
        # we consider stale if older than ~2 mins
        try:
            ts = datetime.strptime(CACHE["cached_at"], "%Y-%m-%d %H:%M:%S")
            age_sec = (datetime.now() - ts).total_seconds()
            stale_flag = age_sec > 120
        except:
            stale_flag = True

    return {
        "status": "ok",
        "token_present": bool(CURRENT_ACCESS_TOKEN),
        "cached_at": CACHE["cached_at"],
        "stale": stale_flag
    }

@app.get("/latest")
def latest():
    """
    Always return last known cache, even if stale
    """
    stale_flag = True
    if CACHE["cached_at"]:
        try:
            ts = datetime.strptime(CACHE["cached_at"], "%Y-%m-%d %H:%M:%S")
            age_sec = (datetime.now() - ts).total_seconds()
            stale_flag = age_sec > 120
        except:
            stale_flag = True

    return {
        "status": "ok",
        "data": {
            "cached_at": CACHE["cached_at"],
            "spot": CACHE["spot"],
            "chain": CACHE["chain"],
            "stale": stale_flag,
            "lot_sizes": CACHE["lot_sizes"]
        }
    }

@app.post("/admin/set_token")
def set_token(req: Request, body: TokenBody):
    global CURRENT_ACCESS_TOKEN
    admin_header = req.headers.get("X-ADMIN-KEY", "")
    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")

    tok = body.access_token.strip()
    if len(tok) < 5:
        raise HTTPException(status_code=400, detail="Bad token")

    CURRENT_ACCESS_TOKEN = tok
    save_token_to_disk(tok)

    print("[TOKEN UPDATED VIA ADMIN]", tok[:10] + "******")
    return {"status": "ok", "message": "token saved and fetch started"}

@app.get("/expiries")
def expiries(instrument: str):
    """
    We will ask Zerodha instruments() once, filter only NIFTY options,
    group by expiry, then return the next 4 expiries in friendly format.
    """
    try:
        kite_local = get_kite_client()
        all_inst = kite_local.instruments("NFO")
    except Exception as e:
        # fallback using last known dates in memory if you want
        # but for now return safe msg
        return {
            "instrument": instrument,
            "expiries": [
                {"label": "28 Oct 2025", "value": "2025-10-28"},
                {"label": "04 Nov 2025", "value": "2025-11-04"},
                {"label": "11 Nov 2025", "value": "2025-11-11"},
                {"label": "18 Nov 2025", "value": "2025-11-18"},
            ]
        }

    # filter only instrument=='NIFTY' or whatever naming we map
    sym = "NIFTY"
    expiry_set = set()
    for row in all_inst:
        if row.get("tradingsymbol","").startswith(sym) and row.get("instrument_type") in ("CE","PE"):
            expiry_set.add(row["expiry"])

    expiry_list = sorted(list(expiry_set))[:4]
    nice = []
    for dt in expiry_list:
        # dt is datetime.date
        label = dt.strftime("%d %b %Y")
        value = dt.strftime("%Y-%m-%d")
        nice.append({"label": label, "value": value})

    return {"instrument": instrument, "expiries": nice}

@app.get("/strikes")
def strikes(instrument: str, spot: float):
    """
    Return strike list for dropdown. Steps of 50 around spot +/-1500.
    Example: spot 25977 → nearest 50 = 25950. range ~ 24450..27450
    We'll clamp to int.
    """
    spot = float(spot)
    base = round(spot / 50.0) * 50  # nearest 50
    out = []
    low = base - 1500
    high = base + 1500
    s = low
    while s <= high:
        out.append(int(s))
        s += 50
    return {"strikes": out}

@app.get("/quote")
def quote(instrument: str, expiry: str, strike: int, type: str):
    """
    Return option price (LTP) + placeholder IV/greeks for now.
    We'll build the tradingsymbol from expiry label.
    instrument should be 'NIFTY' for now.
    """
    try:
        kite_local = get_kite_client()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No access token: {e}")

    try:
        # expiry is label like "28 Oct 2025"
        tsym = build_nifty_option_symbol(expiry, strike, type)
        # Zerodha wants exchange:tradingsymbol e.g. "NFO:NIFTY25O2825900CE"
        full_code = f"NFO:{tsym}"

        q = kite_local.quote([full_code])
        item = q[full_code]

        ltp = item["last_price"]
        iv = item.get("implied_volatility")  # may not be present in quote(), might need instruments/option chain later
        oi = item.get("oi")

        return {
            "ok": True,
            "instrument": instrument,
            "expiry": expiry,
            "strike": strike,
            "type": type.upper(),
            "optionPrice": ltp,
            "iv": iv,
            "oi": oi,
            # greeks we will compute on frontend with BS model
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None
        }

    except Exception as e:
        print("[QUOTE ERROR]", str(e))
        # don't 500 the entire request for frontend
        return {
            "ok": False,
            "instrument": instrument,
            "expiry": expiry,
            "strike": strike,
            "type": type.upper(),
            "optionPrice": None,
            "iv": None,
            "oi": None,
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None,
            "error": str(e)
        }
