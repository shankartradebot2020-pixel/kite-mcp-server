"""
Wraps Zerodha Kite Connect calls and adds the pivot / CPR math needed
for the EMA-crossover + zone-confluence strategy.
"""

import os
import datetime as dt
from kiteconnect import KiteConnect

API_KEY = os.environ.get("KITE_API_KEY")
ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN")  # refreshed daily, see auth.py

_kite = None
_instruments_cache = {"NFO": None, "NSE": None, "loaded_at": None}


def get_kite():
    global _kite
    if _kite is None:
        if not API_KEY:
            raise RuntimeError("KITE_API_KEY not set")
        _kite = KiteConnect(api_key=API_KEY)
        if ACCESS_TOKEN:
            _kite.set_access_token(ACCESS_TOKEN)
    return _kite


def set_access_token(token: str):
    """Call this after the daily login flow produces a fresh token."""
    global ACCESS_TOKEN
    ACCESS_TOKEN = token
    get_kite().set_access_token(token)


# ---------- instrument lookup ----------

def _load_instruments(exchange: str):
    if _instruments_cache[exchange] is None:
        _instruments_cache[exchange] = get_kite().instruments(exchange)
        _instruments_cache["loaded_at"] = dt.datetime.now()
    return _instruments_cache[exchange]


def find_instrument_token(tradingsymbol: str, exchange: str = "NSE"):
    for row in _load_instruments(exchange):
        if row["tradingsymbol"] == tradingsymbol:
            return row["instrument_token"]
    raise ValueError(f"Symbol {tradingsymbol} not found on {exchange}")


# ---------- historical candles ----------

INTERVAL_MAP = {
    "hourly": "60minute",
    "daily": "day",
    "15min": "15minute",
    "5min": "5minute",
}


def get_historical_candles(tradingsymbol: str, timeframe: str, days_back: int = 60, exchange: str = "NSE"):
    interval = INTERVAL_MAP.get(timeframe, timeframe)
    token = find_instrument_token(tradingsymbol, exchange)
    to_date = dt.datetime.now()
    from_date = to_date - dt.timedelta(days=days_back)
    candles = get_kite().historical_data(token, from_date, to_date, interval)
    return candles


# ---------- pivot / CPR calculation ----------

def _pivot_from_hlc(high: float, low: float, close: float):
    """Standard floor pivot formula used for weekly/monthly pivots."""
    p = (high + low + close) / 3
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    bc = (high + low) / 2  # bottom central pivot
    tc = (p - bc) + p      # top central pivot
    return {
        "pivot": round(p, 2),
        "tc": round(tc, 2),
        "bc": round(bc, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
    }


def _aggregate_period(candles, period: str):
    """Roll daily candles up into weekly or monthly OHLC bars."""
    buckets = {}
    for c in candles:
        d = c["date"]
        if period == "week":
            key = d.isocalendar()[:2]  # (iso_year, iso_week)
        else:
            key = (d.year, d.month)
        b = buckets.setdefault(key, {"open": c["open"], "high": c["high"],
                                      "low": c["low"], "close": c["close"], "date": d})
        b["high"] = max(b["high"], c["high"])
        b["low"] = min(b["low"], c["low"])
        b["close"] = c["close"]
        b["date"] = d
    ordered = sorted(buckets.values(), key=lambda x: x["date"])
    return ordered


def get_zone_levels(tradingsymbol: str, exchange: str = "NSE"):
    """
    Returns the confluence zone the strategy cares about:
    previous week high/low, previous month high/low, weekly pivots (S1/R1),
    monthly pivots (S1/R1).
    """
    daily = get_historical_candles(tradingsymbol, "daily", days_back=70, exchange=exchange)
    weeks = _aggregate_period(daily, "week")
    months = _aggregate_period(daily, "month")

    if len(weeks) < 2 or len(months) < 2:
        raise ValueError("Not enough daily history to compute weekly/monthly pivots")

    prev_week = weeks[-2]
    prev_month = months[-2]

    weekly_pivots = _pivot_from_hlc(prev_week["high"], prev_week["low"], prev_week["close"])
    monthly_pivots = _pivot_from_hlc(prev_month["high"], prev_month["low"], prev_month["close"])

    return {
        "symbol": tradingsymbol,
        "PWH": round(prev_week["high"], 2),
        "PWL": round(prev_week["low"], 2),
        "PMH": round(prev_month["high"], 2),
        "PML": round(prev_month["low"], 2),
        "weekly_pivots": weekly_pivots,
        "monthly_pivots": monthly_pivots,
        "support_zone": sorted([
            weekly_pivots["s1"], round(prev_week["low"], 2), round(prev_month["low"], 2)
        ]),
        "resistance_zone": sorted([
            weekly_pivots["r1"], round(prev_week["high"], 2), monthly_pivots["r1"]
        ]),
    }


# ---------- EMA + crossover detection ----------

def _ema(values, period):
    k = 2 / (period + 1)
    ema_vals = [values[0]]
    for v in values[1:]:
        ema_vals.append(v * k + ema_vals[-1] * (1 - k))
    return ema_vals


def detect_ema_crossovers(tradingsymbol: str, exchange: str = "NSE", lookback_days: int = 20):
    """
    Pulls hourly candles and flags 5/20 EMA bullish crossovers,
    so we can check whether the 'twice near the zone' condition is met.
    """
    candles = get_historical_candles(tradingsymbol, "hourly", days_back=lookback_days, exchange=exchange)
    closes = [c["close"] for c in candles]
    if len(closes) < 25:
        raise ValueError("Not enough hourly candles for EMA(20)")

    ema5 = _ema(closes, 5)
    ema20 = _ema(closes, 20)

    crossovers = []
    for i in range(1, len(closes)):
        prev_diff = ema5[i - 1] - ema20[i - 1]
        curr_diff = ema5[i] - ema20[i]
        if prev_diff <= 0 and curr_diff > 0:
            crossovers.append({
                "date": str(candles[i]["date"]),
                "close": candles[i]["close"],
                "type": "bullish_cross",
            })
        elif prev_diff >= 0 and curr_diff < 0:
            crossovers.append({
                "date": str(candles[i]["date"]),
                "close": candles[i]["close"],
                "type": "bearish_cross",
            })
    return crossovers


# ---------- option chain ----------

def get_option_chain(underlying: str, expiry: str):
    """
    underlying: e.g. 'NIFTY', 'RELIANCE'
    expiry: 'YYYY-MM-DD' matching Kite's expiry format
    Returns strikes with LTP + basic Greeks/IV via quote data.
    """
    instruments = _load_instruments("NFO")
    matches = [
        row for row in instruments
        if row["name"] == underlying
        and str(row["expiry"]) == expiry
        and row["instrument_type"] in ("CE", "PE")
    ]
    if not matches:
        raise ValueError(f"No option contracts found for {underlying} expiry {expiry}")

    symbols = [f"NFO:{row['tradingsymbol']}" for row in matches]
    kite = get_kite()

    chain = []
    # Kite quote() allows up to ~500 symbols per call; chunk if needed
    for i in range(0, len(symbols), 200):
        batch = symbols[i:i + 200]
        quotes = kite.quote(batch)
        for sym, q in quotes.items():
            row = next(r for r in matches if f"NFO:{r['tradingsymbol']}" == sym)
            chain.append({
                "tradingsymbol": row["tradingsymbol"],
                "strike": row["strike"],
                "type": row["instrument_type"],
                "ltp": q.get("last_price"),
                "oi": q.get("oi"),
                "volume": q.get("volume"),
                "bid": q["depth"]["buy"][0]["price"] if q.get("depth", {}).get("buy") else None,
                "ask": q["depth"]["sell"][0]["price"] if q.get("depth", {}).get("sell") else None,
            })
    return sorted(chain, key=lambda x: (x["strike"], x["type"]))
