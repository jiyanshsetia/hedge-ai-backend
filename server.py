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

STATE = {
    "access_token": None,
    "last_fetch_ts": None,
    "spot": {},
    "option_cache": {},
    "instrument_dump": [],
    "instrument_last_pull": 0,
    "snapshot_loaded": False,
}

LOT_SIZES = {
    "NIFTY_50": 75,
    "BANKNIFTY": 35,
    "SENSEX": 20,  # ✅ Added correct lot size for SENSEX
}

SNAPSHOT_FILE = "snapshot.json"
INSTRUMENT_REFRESH_SECONDS = 300
APP_START_TIME = time.time()

# -------------------------
# FASTAPI setup
# -------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Helper functions
# -------------------------
def _kite_client() -> KiteConnect:
    if not KITE_API_KEY:
        raise Exception("KITE_API_KEY not configured")
    if not STATE["access_token"]:
        raise Exception("No access token loaded")
    kc = KiteConnect(api_key=KITE_API_KEY)
    kc.set_access_token(STATE["access_token"])
    return kc


def _save_snapshot_to_disk():
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
    return dt.strftime("%d %b %Y")


def _maybe_pull_instruments():
    now = time.time()
    if now - STATE["instrument_last_pull"] < INSTRUMENT_REFRESH_SECONDS:
        return
    try:
        kc = _kite_client()
        instruments = kc.instruments("NFO")
        STATE["instrument_dump"] = instruments
        STATE["instrument_last_pull"] = now
        print("[INSTRUMENTS] pulled", len(instruments))
    except Exception as e:
        print("[INSTRUMENTS ERROR]", e)


def _map_underlying(instrument_name: str) -> str:
    """✅ Unified mapping for NIFTY, BANKNIFTY, SENSEX"""
    if instrument_name == "NIFTY_50":
        return "NIFTY"
    elif instrument_name == "BANKNIFTY":
        return "BANKNIFTY"
    elif instrument_name == "SENSEX":
        return "SENSEX"
    else:
        return instrument_name


def _build_expiry_list_for_symbol(instrument_name: str) -> List[Dict[str, str]]:
    underlying = _map_underlying(instrument_name)
    expiries = set()
    for row in STATE["instrument_dump"]:
        try:
            if row.get("segment") == "NFO-OPT" and row.get("name") == underlying:
                exp_obj = row["expiry"]
                exp_dt = datetime.fromisoformat(exp_obj) if isinstance(exp_obj, str) else datetime.combine(exp_obj, datetime.min.time())
                expiries.add(exp_dt)
        except:
            continue
    expiries_sorted = sorted(expiries)[:4]
    return [{"label": _parse_expiry_label(dt_exp), "value": dt_exp.date().isoformat()} for dt_exp in expiries_sorted]


def _build_strike_list(instrument_name: str, expiry_iso: str) -> List[int]:
    underlying = _map_underlying(instrument_name)
    try:
        expiry_target = datetime.fromisoformat(expiry_iso).date()
    except:
        return []
    strikes = set()
    for row in STATE["instrument_dump"]:
        try:
            if row.get("segment") == "NFO-OPT" and row.get("name") == underlying:
                row_exp = row["expiry"]
                row_exp_date = datetime.fromisoformat(row_exp).date() if isinstance(row_exp, str) else row_exp
                if row_exp_date != expiry_target:
                    continue
                strike_val = row.get("strike")
                if strike_val:
                    strikes.add(int(round(float(strike_val))))
        except:
            continue
    sorted_strikes = sorted(strikes)
    spot_val = STATE["spot"].get(instrument_name)
    if spot_val:
        lower, upper = spot_val - 1500, spot_val + 1500
        sorted_strikes = [s for s in sorted_strikes if lower <= s <= upper]
    return sorted_strikes


def _find_tradingsymbol(instrument_name: str, expiry_iso: str, strike: float, opt_type: str) -> Optional[str]:
    underlying = _map_underlying(instrument_name)
    try:
        expiry_target = datetime.fromisoformat(expiry_iso).date()
    except:
        return None
    for row in STATE["instrument_dump"]:
        try:
            if row.get("segment") == "NFO-OPT" and row.get("name") == underlying:
                row_exp = row["expiry"]
                row_exp_date = datetime.fromisoformat(row_exp).date() if isinstance(row_exp, str) else row_exp
                if row_exp_date != expiry_target:
                    continue
                if row.get("instrument_type") == opt_type and int(round(float(row.get("strike", 0)))) == int(round(float(strike))):
                    return row.get("tradingsymbol")
        except:
            continue
    return None


def _quote_option(tradingsymbol: str) -> Dict[str, Any]:
    try:
        kc = _kite_client()
        full_symbol = "NFO:" + tradingsymbol
        q = kc.quote([full_symbol])
        data = q[full_symbol]
        out = {
            "tradingsymbol": tradingsymbol,
            "option_price": data.get("last_price"),
            "iv": data.get("implied_volatility"),
            "delta": data.get("delta"),
            "theta": data.get("theta"),
            "gamma": data.get("gamma"),
            "vega": data.get("vega"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        STATE["option_cache"][tradingsymbol] = out
        return out
    except Exception as e:
        print("[QUOTE ERROR]", tradingsymbol, e)
        cached = STATE["option_cache"].get(tradingsymbol)
        if cached:
            cached["stale"] = True
            return cached
        raise HTTPException(status_code=500, detail="quote failed and no cache")


def _fetch_spot_loop():
    while True:
        try:
            _maybe_pull_instruments()
            if not STATE["access_token"]:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            kc = _kite_client()
            mapping = {
                "NIFTY_50": "NSE:NIFTY 50",
                "BANKNIFTY": "NSE:BANKNIFTY",
                "SENSEX": "BSE:SENSEX",  # ✅ Added live Sensex feed
            }
            q = kc.quote(list(mapping.values()))
            new_spot = {k: q[v]["last_price"] for k, v in mapping.items() if v in q}
            if new_spot:
                STATE["spot"] = new_spot
                STATE["last_fetch_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print("[FETCH OK]", STATE["last_fetch_ts"], STATE["spot"])
                _save_snapshot_to_disk()
        except Exception as e:
            print("[FETCH ERROR]", e)
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)

# -------------------------
# ROUTES
# -------------------------
class AdminTokenBody(BaseModel):
    access_token: str


@app.on_event("startup")
def on_startup():
    _load_snapshot_from_disk()
    if os.path.exists("access_token.json"):
        try:
            with open("access_token.json", "r") as f:
                data = json.load(f)
            STATE["access_token"] = data.get("access_token")
        except Exception as e:
            print("[BOOT TOKEN RESTORE ERROR]", e)
    threading.Thread(target=_fetch_spot_loop, daemon=True).start()
    print("[THREAD] fetch_market_data_loop started")


@app.get("/")
def root():
    return {"message": "HedgeAI backend online", "token_present": bool(STATE["access_token"])}


def _is_stale() -> bool:
    if not STATE["last_fetch_ts"]:
        return True
    try:
        ts = datetime.strptime(STATE["last_fetch_ts"], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - ts).total_seconds() > 120
    except:
        return True


@app.get("/latest")
def latest():
    return {"status": "ok", "data": {"cached_at": STATE["last_fetch_ts"], "spot": STATE["spot"], "lot_sizes": LOT_SIZES, "stale": _is_stale()}}


@app.get("/expiries")
def get_expiries(instrument: str):
    _maybe_pull_instruments()
    if not STATE["instrument_dump"]:
        raise HTTPException(status_code=500, detail="no instruments cache yet")
    return {"instrument": instrument, "expiries": _build_expiry_list_for_symbol(instrument)}


@app.get("/strikes")
def get_strikes(instrument: str, expiry: str):
    _maybe_pull_instruments()
    if not STATE["instrument_dump"]:
        raise HTTPException(status_code=500, detail="no instruments cache yet")
    return {"instrument": instrument, "expiry": expiry, "strikes": _build_strike_list(instrument, expiry)}


@app.get("/option_quote")
def option_quote(instrument: str, expiry: str, strike: float, opt_type: str):
    _maybe_pull_instruments()
    tsym = _find_tradingsymbol(instrument, expiry, strike, opt_type)
    if not tsym:
        raise HTTPException(status_code=404, detail="No matching option contract")
    q = _quote_option(tsym)
    q.update({"instrument": instrument, "expiry": expiry, "strike": strike, "opt_type": opt_type, "spot_now": STATE["spot"].get(instrument), "lot_size": LOT_SIZES.get(instrument), "stale": _is_stale()})
    return q


@app.get("/futures_quote")  # ✅ NEW endpoint
def futures_quote(instrument: str):
    """
    Example:
      /futures_quote?instrument=NIFTY_50
      /futures_quote?instrument=SENSEX
    """
    try:
        _maybe_pull_instruments()
        kc = _kite_client()
        underlying = _map_underlying(instrument)
        fut_contract = None
        for row in STATE["instrument_dump"]:
            if row.get("segment") == "NFO-FUT" and row.get("name") == underlying:
                fut_contract = "NFO:" + row["tradingsymbol"]
                break
        if not fut_contract:
            raise HTTPException(status_code=404, detail="No futures contract found")
        q = kc.quote([fut_contract])
        fut_price = q[fut_contract]["last_price"]
        return {"instrument": instrument, "fut_symbol": fut_contract, "fut_price": fut_price, "lot_size": LOT_SIZES.get(instrument), "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        print("[/futures_quote ERROR]", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="futures quote failed")


@app.post("/admin/set_token")
async def set_token(request: Request, body: AdminTokenBody):
    admin_header = request.headers.get("X-ADMIN-KEY", "")
    if admin_header != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized admin key")
    token = body.access_token.strip()
    STATE["access_token"] = token
    with open("access_token.json", "w") as f:
        json.dump({"access_token": token}, f)
    STATE["instrument_last_pull"] = 0
    print("[TOKEN UPDATED]")
    return {"status": "ok", "message": "token saved"}
