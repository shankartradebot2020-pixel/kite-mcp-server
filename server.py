import os
import json
from mcp.server import Server
from mcp.server.streamable_http import streamable_http_app
import mcp.types as types
from starlette.applications import Starlette
from starlette.routing import Route, Mount

import kite_helper
import auth

app = Server("zerodha-trading-tools")


@app.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="get_historical_candles",
            description="Get OHLC candle data for a symbol (hourly/daily) for technical analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tradingsymbol": {"type": "string", "description": "e.g. RELIANCE, NIFTY 50"},
                    "timeframe": {"type": "string", "enum": ["hourly", "daily", "15min", "5min"]},
                    "days_back": {"type": "integer", "default": 60},
                    "exchange": {"type": "string", "default": "NSE"},
                },
                "required": ["tradingsymbol", "timeframe"],
            },
        ),
        types.Tool(
            name="get_zone_levels",
            description=(
                "Get the confluence support/resistance zone for a symbol: "
                "previous week high/low (PWH/PWL), previous month high/low (PMH/PML), "
                "and weekly + monthly CPR pivots (S1/R1/etc). Matches the EMA-crossover "
                "zone strategy."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tradingsymbol": {"type": "string"},
                    "exchange": {"type": "string", "default": "NSE"},
                },
                "required": ["tradingsymbol"],
            },
        ),
        types.Tool(
            name="detect_ema_crossovers",
            description=(
                "Detect 5/20 EMA crossovers on hourly candles for a symbol, to check "
                "whether the 'crossed twice near the zone' entry condition is met."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tradingsymbol": {"type": "string"},
                    "exchange": {"type": "string", "default": "NSE"},
                    "lookback_days": {"type": "integer", "default": 20},
                },
                "required": ["tradingsymbol"],
            },
        ),
        types.Tool(
            name="get_option_chain",
            description=(
                "Get the option chain (strikes, LTP, OI, bid/ask) for an underlying and expiry, "
                "used to check premium/IV before entering so you don't overpay."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "underlying": {"type": "string", "description": "e.g. NIFTY, RELIANCE"},
                    "expiry": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["underlying", "expiry"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "get_historical_candles":
            result = kite_helper.get_historical_candles(
                arguments["tradingsymbol"],
                arguments["timeframe"],
                arguments.get("days_back", 60),
                arguments.get("exchange", "NSE"),
            )
        elif name == "get_zone_levels":
            result = kite_helper.get_zone_levels(
                arguments["tradingsymbol"], arguments.get("exchange", "NSE")
            )
        elif name == "detect_ema_crossovers":
            result = kite_helper.detect_ema_crossovers(
                arguments["tradingsymbol"],
                arguments.get("exchange", "NSE"),
                arguments.get("lookback_days", 20),
            )
        elif name == "get_option_chain":
            result = kite_helper.get_option_chain(
                arguments["underlying"], arguments["expiry"]
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=json.dumps(result, default=str))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


mcp_asgi_app = app.streamable_http_app()

routes = [
    Route("/login", auth.login),
    Route("/callback", auth.callback),
    Mount("/mcp", app=mcp_asgi_app),
]

starlette_app = Starlette(routes=routes)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
