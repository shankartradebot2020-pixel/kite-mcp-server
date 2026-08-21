import os
import json
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route, Mount

import kite_helper
import auth

mcp = FastMCP("zerodha-trading-tools")


@mcp.tool()
def get_historical_candles(tradingsymbol: str, timeframe: str, days_back: int = 60, exchange: str = "NSE") -> str:
    """
    Get OHLC candle data for a symbol.
    timeframe: hourly, daily, 15min, 5min
    """
    result = kite_helper.get_historical_candles(tradingsymbol, timeframe, days_back, exchange)
    return json.dumps(result, default=str)


@mcp.tool()
def get_zone_levels(tradingsymbol: str, exchange: str = "NSE") -> str:
    """
    Get the confluence support/resistance zone for a symbol:
    PWH, PWL, PMH, PML, weekly CPR (S1/R1), monthly CPR (S1/R1).
    This is the zone where we watch for the 5/20 EMA crossover setup.
    """
    result = kite_helper.get_zone_levels(tradingsymbol, exchange)
    return json.dumps(result, default=str)


@mcp.tool()
def detect_ema_crossovers(tradingsymbol: str, exchange: str = "NSE", lookback_days: int = 20) -> str:
    """
    Detect 5/20 EMA bullish and bearish crossovers on hourly candles.
    Used to check if the 'crossed twice near the zone' entry condition is met.
    """
    result = kite_helper.detect_ema_crossovers(tradingsymbol, exchange, lookback_days)
    return json.dumps(result, default=str)


@mcp.tool()
def get_option_chain(underlying: str, expiry: str) -> str:
    """
    Get option chain for an underlying and expiry (YYYY-MM-DD).
    Returns strikes, LTP, OI, bid/ask to help evaluate premium before entry.
    """
    result = kite_helper.get_option_chain(underlying, expiry)
    return json.dumps(result, default=str)


# Mount login routes alongside MCP
mcp_app = mcp.http_app(path="/mcp")

routes = [
    Route("/login", auth.login),
    Route("/callback", auth.callback),
    Mount("/", app=mcp_app),
]

starlette_app = Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
