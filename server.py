import os
import time
import json
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# -------------------------
# CONFIG / GLOBAL STATE
# -------------------------
load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY", "CHANGE_ME")
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# in-memory runtime state
STATE = {
    "access_token": None,       # Zerodha access_token (refresh daily)
    "last_fetch_ts": None,      # "2025-10-27 06:02:10"
    "spot": {},                 # {"NIFTY_50": 25977.4, "BANKNIFTY": 51234.5}
    "option_cache": {},         # per tradingsymbol last good quote
    "instrument_dump": [],      # list of NFO instruments from kite.instruments("NFO")
    "instrument_last_pull": 0,  # epoch seconds of last pull for instruments
    "snapshot_loaded": False,
}

# hardcoded lot sizes (futures & options market lot sizes)
LOT_SIZES = {
    "NIFTY_50": 75,
    "BANKNIFTY": 35,
    "SENSEX": 20,  # ✅ Correct lot size for SENSEX
}

SNAPSHOT_FILE = "snapshot.json"
INSTRUMENT_REFRESH_SECONDS = 300  # refresh contract list every 5 min
APP_START_TIME = time.time()

# -------------------------
# FASTAPI init + CORS
# -------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Shopify storefront will iframe / embed from a different domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Helpers
# -------------------------

def _kite_client() -> KiteConnect:
    """
    Returns a KiteConnect client with api_key and access_token injected.
    Raises if we don't have access_token yet.
    """
    if not KITE_API_KEY:
        raise Exception("KITE_API_KEY not configured")
    if not STATE["access_token"]:
        raise Exception("No access token loaded")

    kc = KiteConnect(api_key=KITE_API_KEY)
    kc.set_access_token(STATE["access_token"])
    return kc


def _save_snapshot_to_disk():
    """
    Write snapshot of (spot, last_fetch_ts, option_cache minimal) to disk,
    so Render cold start can still answer /latest without live fetch.
    """
    try:
        snap = {
            "last_fetch_ts": STATE["last_fetch_ts"],
            "spot": STATE["spot"],
            "option_cache": STATE["option_cache"],
        }
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
        print("[SNAPSHOT SAVED]")
    except Exception as e:
        print("[SNAPSHOT SAVE ERROR]", e)


def _load_snapshot_from_disk():
    """
    On boot, try to recover last known data.
    """
    if STATE["snapshot_loaded"]:
        return
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r") as f:
                data = json.load(f)
            STATE["last_fetch_ts"] = data.get("last_fetch_ts")
            STATE["spot"] = data.get("spot", {})
            STATE["option_cache"] = data.get("option_cache", {})
            STATE["snapshot_loaded"] = True
            print("[SNAPSHOT LOADED]", STATE["last_fetch_ts"], STATE["spot"])
        else:
            print("[SNAPSHOT] no snapshot file yet")
    except Exception as e:
        print("[SNAPSHOT LOAD ERROR]", e)


def _parse_expiry_label(dt: datetime) -> str:
    # Show human label like "28 Oct 2025"
    return dt.strftime("%d %b %Y")


def _maybe_pull_instruments():
    """
    Pull the full NFO instruments (option chain definitions) from Zerodha.
    Cache in STATE["instrument_dump"].
    We'll attempt this every INSTRUMENT_REFRESH_SECONDS, and skip if token missing.
    """
    now = time.time()
    if now - STATE["instrument_last_pull"] < INSTRUMENT_REFRESH_SECONDS:
        return  # recently pulled

    try:
        kc = _kite_client()
        instruments = kc.instruments("NFO")  # big list of dicts
        STATE["instrument_dump"] = instruments
        STATE["instrument_last_pull"] = now
        print("[INSTRUMENTS] pulled", len(instruments))
    except Exception as e:
        # token might be stale or market down. Keep old list.
        print("[INSTRUMENTS ERROR]", e)


def _build_expiry_list_for_symbol(instrument_name: str) -> List[Dict[str, str]]:
    """
    instrument_name: "NIFTY_50" or "BANKNIFTY" (our frontend naming)
      map to actual underlying tradingsymbol in instrument list.
      For now:
        NIFTY_50 -> "NIFTY"
        BANKNIFTY -> "BANKNIFTY"

    Return sorted unique expiries for that index from STATE["instrument_dump"].
    """
if instrument_name == "NIFTY_50":
    underlying = "NIFTY"
elif instrument_name == "BANKNIFTY":
    underlying = "BANKNIFTY"
elif instrument_name == "SENSEX":
    underlying = "SENSEX"  # ✅ Added Sensex mapping
else:
    underlying = instrument_name
    expiries = set()
    for row in STATE["instrument_dump"]:
        try:
            if row.get("segment") == "NFO-OPT" and row.get("name") == underlying:
                # row["expiry"] is datetime.date or string? Usually datetime.date
                exp_obj = row["expiry"]
                if isinstance(exp_obj, str):
                    # just in case it's string "2025-10-30"
                    exp_dt = datetime.fromisoformat(exp_obj)
                else:
                    # date -> convert to datetime midnight
                    exp_dt = datetime.combine(exp_obj, datetime.min.time())

                expiries.add(exp_dt)
        except:
            continue

    # sort ascending
    expiries_sorted = sorted(expiries)
    # limit to next 4 expiries
    expiries_sorted = expiries_sorted[:4]

    out = []
    for dt_exp in expiries_sorted:
        out.append({
            "label": _parse_expiry_label(dt_exp),
            "value": dt_exp.date().isoformat()  # "2025-10-30"
        })
    return out


def _build_strike_list(instrument_name: str, expiry_iso: str) -> List[int]:
    """
    Return sorted strikes for that instrument+expiry (unique strike prices)
    Filter to something sensible like +/- 1000 points around spot,
    and step 50 for NIFTY, 100 for BANKNIFTY etc.
    """
    underlying = "NIFTY" if instrument_name == "NIFTY_50" else "BANKNIFTY"

    try:
        expiry_target = datetime.fromisoformat(expiry_iso).date()
    except:
        return []

    strikes = set()
    for row in STATE["instrument_dump"]:
        try:
            if (
                row.get("segment") == "NFO-OPT"
                and row.get("name") == underlying
            ):
                row_exp = row["expiry"]
                if isinstance(row_exp, str):
                    row_exp_date = datetime.fromisoformat(row_exp).date()
                else:
                    row_exp_date = row_exp
                if row_exp_date != expiry_target:
                    continue

                strike_val = row.get("strike")
                if strike_val is None:
                    continue
                # Only keep int-like
                strikes.add(int(round(float(strike_val))))
        except:
            continue

    # sort strikes
    sorted_strikes = sorted(strikes)

    # Option: restrict to "nice" band around current spot so dropdown not insane huge
    spot_val = None
    if instrument_name in STATE["spot"]:
        spot_val = STATE["spot"][instrument_name]

    if spot_val:
        lower = spot_val - 1500
        upper = spot_val + 1500
        filtered = [s for s in sorted_strikes if (s >= lower and s <= upper)]
        if filtered:
            sorted_strikes = filtered

    return sorted_strikes


def _find_tradingsymbol(instrument_name: str, expiry_iso: str, strike: float, opt_type: str) -> Optional[str]:
    """
    Find the matching contract's tradingsymbol in STATE["instrument_dump"]
    opt_type = "CE" or "PE"
    """
    underlying = "NIFTY" if instrument_name == "NIFTY_50" else "BANKNIFTY"

    try:
        expiry_target = datetime.fromisoformat(expiry_iso).date()
    except:
        return None

    best = None
    for row in STATE["instrument_dump"]:
        try:
            if row.get("segment") != "NFO-OPT":
                continue
            if row.get("name") != underlying:
                continue

            row_exp = row["expiry"]
            if isinstance(row_exp, str):
                row_exp_date = datetime.fromisoformat(row_exp).date()
            else:
                row_exp_date = row_exp

            if row_exp_date != expiry_target:
                continue

            st = row.get("strike")
            oi = row.get("instrument_type")  # "CE"/"PE"

            if oi == opt_type and st is not None:
                # compare strike as int
                if int(round(float(st))) == int(round(float(strike))):
                    best = row.get("tradingsymbol")
                    break
        except:
            continue

    return best


def _quote_option(tradingsymbol: str) -> Dict[str, Any]:
    """
    Call kite.quote("NFO:<symbol>") to get last traded price (option premium).
    We also store in STATE["option_cache"][tradingsymbol"] so if future calls fail,
    we can still serve last good data.
    """
    # try live fetch
    try:
        kc = _kite_client()
        full_symbol = "NFO:" + tradingsymbol
        q = kc.quote([full_symbol])
        data = q[full_symbol]

        last_price = data.get("last_price")
        # Zerodha REST quote() doesn't always return greeks.
        # We'll store what we have.
        out = {
            "tradingsymbol": tradingsymbol,
            "option_price": last_price,
            "iv": data.get("implied_volatility"),  # may be None
            "delta": data.get("delta"),
            "theta": data.get("theta"),
            "gamma": data.get("gamma"),
            "vega": data.get("vega"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        STATE["option_cache"][tradingsymbol] = out
        return out

    except Exception as e:
        print("[QUOTE ERROR]", tradingsymbol, e)
        # fallback to cache
        cached = STATE["option_cache"].get(tradingsymbol)
        if cached:
            fail_out = dict(cached)
            fail_out["stale"] = True
            return fail_out

        raise HTTPException(status_code=500, detail="quote failed and no cache")


def _fetch_spot_loop():
    """
    Background thread:
    - Pull index spot from quote()
    - Save STATE["spot"] and STATE["last_fetch_ts"]
    - Refresh instruments every ~5 min
    - Save snapshot.json
    """
    while True:
        try:
            # refresh instruments list occasionally
            _maybe_pull_instruments()

            if not STATE["access_token"]:
                # can't hit Kite live, just sleep
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            kc = _kite_client()

            quotes_req = []
            # map our naming -> Zerodha indices
            # NIFTY_50 -> NSE:NIFTY 50
            # BANKNIFTY -> NSE:BANKNIFTY
            mapping = {
    "NIFTY_50": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:BANKNIFTY",
    "SENSEX": "BSE:SENSEX",  # ✅ Added Sensex live spot
}
            for k, v in mapping.items():
                quotes_req.append(v)

            q = kc.quote(quotes_req)

            new_spot = {}
            for ui_name, kite_name in mapping.items():
                if kite_name in q and "last_price" in q[kite_name]:
                    new_spot[ui_name] = q[kite_name]["last_price"]

            if new_spot:
                STATE["spot"] = new_spot
                STATE["last_fetch_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print("[FETCH OK]", STATE["last_fetch_ts"], STATE["spot"])
                _save_snapshot_to_disk()
            else:
                print("[FETCH WARN] got no spot data")

        except Exception as e:
            print("[FETCH ERROR]", e)
            traceback.print_exc()

        time.sleep(POLL_INTERVAL_SECONDS)


# -------------------------
# Pydantic models
# -------------------------

class AdminTokenBody(BaseModel):
    access_token: str


# -------------------------
# ROUTES
# -------------------------

@app.on_event("startup")
def on_startup():
    # load snapshot for cold start
    _load_snapshot_from_disk()
    # also load persisted token if we wrote one earlier
    if os.path.exists("access_token.json"):
        try:
            with open("access_token.json", "r") as f:
                data = json.load(f)
            if "access_token" in data:
                STATE["access_token"] = data["access_token"]
                print("[BOOT] access_token restored from file.")
        except Exception as e:
            print("[BOOT] can't restore token:", e)

    # start background fetch thread
    t = threading.Thread(target=_fetch_spot_loop, daemon=True)
    t.start()
    print("[THREAD] fetch_market_data_loop started")


@app.get("/")
def root():
    return {
        "message": "HedgeAI backend online",
        "token_present": bool(STATE["access_token"]),
        "last_fetch_ts": STATE["last_fetch_ts"],
        "uptime_sec": int(time.time() - APP_START_TIME),
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "token_present": bool(STATE["access_token"]),
        "cached_at": STATE["last_fetch_ts"],
        "stale": _is_stale(),
    }
@app.get("/futures_quote")
def futures_quote(instrument: str):
    """
    ?instrument=NIFTY_50 or SENSEX
    returns {
      "instrument": "NIFTY_50",
      "fut_price": 26250.5,
      "lot_size": 75,
      "timestamp": ...
    }
    """
    try:
        kc = _kite_client()
        if instrument == "NIFTY_50":
            symbol = "NFO:NIFTY24NOVFUT"
        elif instrument == "SENSEX":
            symbol = "NFO:SENSEX24NOVFUT"
        else:
            raise HTTPException(status_code=400, detail="unsupported instrument")

        q = kc.quote([symbol])
        fut_price = q[symbol]["last_price"]
        return {
            "instrument": instrument,
            "fut_price": fut_price,
            "lot_size": LOT_SIZES.get(instrument),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print("[FUTURE QUOTE ERROR]", e)
        raise HTTPException(status_code=500, detail="futures quote failed")

def _is_stale() -> bool:
    """
    Mark data stale if last_fetch_ts is older than ~2 min.
    """
    if not STATE["last_fetch_ts"]:
        return True
    try:
        ts = datetime.strptime(STATE["last_fetch_ts"], "%Y-%m-%d %H:%M:%S")
    except:
        return True
    age = datetime.now() - ts
    return age.total_seconds() > 120


@app.get("/latest")
def latest():
    """
    Returns spot, lot sizes, last fetch time, and stale flag.
    Frontend uses this to show:
      - Index Spot
      - Lot Size
      - Whether data is fresh
    """
    out = {
        "cached_at": STATE["last_fetch_ts"],
        "spot": STATE["spot"],
        "lot_sizes": LOT_SIZES,
        "stale": _is_stale(),
    }
    # If we literally have nothing, keep a nice fallback
    if not STATE["spot"]:
        return {
            "status": "ok",
            "data": out,
            "note": "no live spot yet, using snapshot/offline"
        }
    return {"status": "ok", "data": out}


@app.get("/expiries")
def get_expiries(instrument: str):
    """
    ?instrument=NIFTY_50
    returns next ~4 expiries like:
    [
      {"label":"28 Oct 2025","value":"2025-10-28"},
      {"label":"04 Nov 2025","value":"2025-11-04"},
      ...
    ]
    """
    _maybe_pull_instruments()
    if not STATE["instrument_dump"]:
        raise HTTPException(status_code=500, detail="no instruments cache yet")

    expiries = _build_expiry_list_for_symbol(instrument)
    return {
        "instrument": instrument,
        "expiries": expiries
    }


@app.get("/strikes")
def get_strikes(instrument: str, expiry: str):
    """
    ?instrument=NIFTY_50&expiry=2025-10-30
    returns [25600,25650,...]
    """
    _maybe_pull_instruments()
    if not STATE["instrument_dump"]:
        raise HTTPException(status_code=500, detail="no instruments cache yet")

    strikes = _build_strike_list(instrument, expiry)
    return {
        "instrument": instrument,
        "expiry": expiry,
        "strikes": strikes
    }


@app.get("/option_quote")
def option_quote(instrument: str, expiry: str, strike: float, opt_type: str):
    """
    ?instrument=NIFTY_50&expiry=2025-10-30&strike=25900&opt_type=CE
    returns {
      "tradingsymbol": "...",
      "option_price": 112.5,
      "iv": 13.2,
      "delta": 0.42,
      "theta": -18.5,
      "gamma": 0.0021,
      "vega": 5.3,
      "lot_size": 75,
      "spot_now": 25977.4,
      "stale": false
    }
    """
    _maybe_pull_instruments()
    if not STATE["instrument_dump"]:
        raise HTTPException(status_code=500, detail="no instruments cache yet")

    tsym = _find_tradingsymbol(instrument, expiry, strike, opt_type)
    if not tsym:
        raise HTTPException(status_code=404, detail="No matching option contract")

    try:
        q = _quote_option(tsym)
    except HTTPException as e:
        # bubble up HTTPException directly
        raise e
    except Exception as e:
        print("[option_quote ERROR]", e)
        raise HTTPException(status_code=500, detail="quote failed")

    # include context for frontend
    q["instrument"] = instrument
    q["expiry"] = expiry
    q["strike"] = strike
    q["opt_type"] = opt_type
    q["spot_now"] = STATE["spot"].get(instrument)
    q["lot_size"] = LOT_SIZES.get(instrument)

    # stale if our global spot data is stale
    q["stale"] = _is_stale()

    return q


@app.post("/admin/set_token")
async def set_token(request: Request, body: AdminTokenBody):
    """
    Admin route:
    - header: X-ADMIN-KEY must match ADMIN_KEY
    - body: { "access_token": "<Zerodha access token>" }
    Saves token in memory AND writes access_token.json so it persists restarts.
    """
    admin_header = request.headers.get("X-ADMIN-KEY", "")
    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")

    token = body.access_token.strip()
    if not token or len(token) < 5:
        raise HTTPException(status_code=400, detail="Bad token")

    STATE["access_token"] = token
    print("[TOKEN UPDATED VIA ADMIN]", token[:10] + "******")

    # persist token to disk so Render restart can reload
    try:
        with open("access_token.json", "w") as f:
            json.dump({"access_token": token}, f)
        print("[TOKEN SAVED TO DISK]")
    except Exception as e:
        print("[TOKEN DISK SAVE ERROR]", e)

    # force refresh instruments next request
    STATE["instrument_last_pull"] = 0

    return {"status": "ok", "message": "token saved and fetch started"}
