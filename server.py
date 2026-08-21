import os
import json
import secrets
import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import BearerAuthProvider
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import RedirectResponse, PlainTextResponse, JSONResponse
from starlette.requests import Request

import kite_helper

API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
BASE_URL = os.environ.get("BASE_URL", "https://kite-mcp-server-qgsu.onrender.com")

# Simple in-memory token store
# Maps our_token -> kite_access_token
token_store = {}
# Maps state -> None (for CSRF protection)
pending_states = {}


# ---------- OAuth 2.0 endpoints (what Claude talks to) ----------

async def oauth_metadata(request: Request):
    """Claude fetches this to discover our OAuth endpoints."""
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    })


async def oauth_authorize(request: Request):
    """
    Claude redirects user here to start login.
    We forward them to Zerodha login, carrying our state.
    """
    state = request.query_params.get("state", secrets.token_urlsafe(16))
    redirect_uri = request.query_params.get("redirect_uri", "")
    pending_states[state] = {"redirect_uri": redirect_uri}

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()
    # Append state so we get it back in callback
    login_url += f"&state={state}"
    return RedirectResponse(login_url)


async def kite_callback(request: Request):
    """
    Zerodha redirects here after login with request_token.
    We exchange it for access_token, generate our own code,
    then redirect back to Claude with that code.
    """
    request_token = request.query_params.get("request_token")
    state = request.query_params.get("state", "")

    if not request_token:
        return PlainTextResponse("Missing request_token", status_code=400)

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    kite_helper.set_access_token(access_token)

    # Generate a short-lived code Claude will exchange for a bearer token
    code = secrets.token_urlsafe(32)
    token_store[code] = access_token

    # Redirect back to Claude
    pending = pending_states.pop(state, {})
    redirect_uri = pending.get("redirect_uri", "")
    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}")
    return PlainTextResponse("Login successful! You can close this tab.")


async def oauth_token(request: Request):
    """
    Claude POSTs here to exchange the code for a bearer token.
    """
    body = await request.form()
    code = body.get("code")
    if not code or code not in token_store:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token = token_store.pop(code)
    bearer = secrets.token_urlsafe(32)
    token_store[bearer] = access_token
    kite_helper.set_access_token(access_token)

    return JSONResponse({
        "access_token": bearer,
        "token_type": "bearer",
        "expires_in": 86400,
    })


# ---------- MCP tools ----------

mcp = FastMCP("zerodha-trading-tools")


@mcp.tool()
def get_historical_candles(tradingsymbol: str, timeframe: str, days_back: int = 60, exchange: str = "NSE") -> str:
    """Get OHLC candle data. timeframe: hourly, daily, 15min, 5min"""
    result = kite_helper.get_historical_candles(tradingsymbol, timeframe, days_back, exchange)
    return json.dumps(result, default=str)


@mcp.tool()
def get_zone_levels(tradingsymbol: str, exchange: str = "NSE") -> str:
    """
    Get confluence support/resistance zone: PWH, PWL, PMH, PML,
    weekly CPR (S1/R1), monthly CPR (S1/R1).
    """
    result = kite_helper.get_zone_levels(tradingsymbol, exchange)
    return json.dumps(result, default=str)


@mcp.tool()
def detect_ema_crossovers(tradingsymbol: str, exchange: str = "NSE", lookback_days: int = 20) -> str:
    """Detect 5/20 EMA bullish/bearish crossovers on hourly candles."""
    result = kite_helper.detect_ema_crossovers(tradingsymbol, exchange, lookback_days)
    return json.dumps(result, default=str)


@mcp.tool()
def get_option_chain(underlying: str, expiry: str) -> str:
    """Get option chain (strikes, LTP, OI, bid/ask) for underlying + expiry (YYYY-MM-DD)."""
    result = kite_helper.get_option_chain(underlying, expiry)
    return json.dumps(result, default=str)


# ---------- App assembly ----------

mcp_app = mcp.http_app(path="/mcp")

routes = [
    Route("/.well-known/oauth-authorization-server", oauth_metadata),
    Route("/oauth/authorize", oauth_authorize),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Route("/callback", kite_callback),
    Mount("/", app=mcp_app),
]

starlette_app = Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
