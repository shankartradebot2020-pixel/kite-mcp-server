import os
import json
import secrets
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import RedirectResponse, PlainTextResponse, JSONResponse
from starlette.requests import Request

import kite_helper

API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
BASE_URL = os.environ.get("BASE_URL", "https://kite-mcp-server-qgsu.onrender.com")

# In-memory stores
auth_codes = {}      # code -> {access_token, redirect_uri, code_challenge}
bearer_tokens = {}   # bearer -> kite_access_token
registered_clients = {}  # client_id -> client metadata
pending_auth = {}    # state -> {redirect_uri, client_id, code_challenge, code_challenge_method}


# ---------- OAuth Discovery Endpoints ----------

async def protected_resource_metadata(request: Request):
    """RFC 9728 — tells Claude this MCP server requires auth and where to find the auth server."""
    return JSONResponse({
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["trading"],
    })


async def oauth_server_metadata(request: Request):
    """RFC 8414 — Claude discovers our OAuth endpoints here."""
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


# ---------- Dynamic Client Registration (RFC 7591) ----------

async def oauth_register(request: Request):
    """Claude auto-registers itself here before starting the auth flow."""
    body = await request.json()
    client_id = f"claude_{secrets.token_urlsafe(16)}"
    registered_clients[client_id] = {
        "client_id": client_id,
        "redirect_uris": body.get("redirect_uris", []),
        "client_name": body.get("client_name", "Claude"),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": 0,
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


# ---------- OAuth Authorization Flow ----------

async def oauth_authorize(request: Request):
    """Claude redirects user here — we forward to Zerodha login."""
    state = request.query_params.get("state", secrets.token_urlsafe(16))
    redirect_uri = request.query_params.get("redirect_uri", "")
    client_id = request.query_params.get("client_id", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "plain")

    pending_auth[state] = {
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    # Zerodha doesn't support passing custom state, so we store it server-side
    login_url = kite.login_url()
    # Store state in a temp mapping keyed by a session token in the URL
    session = secrets.token_urlsafe(8)
    pending_auth[f"session_{session}"] = state
    # Append session to redirect so callback knows which state to use
    return RedirectResponse(f"{login_url}&state={session}")


async def kite_callback(request: Request):
    """Zerodha posts back here after login."""
    request_token = request.query_params.get("request_token")
    session = request.query_params.get("state", "")

    if not request_token:
        return PlainTextResponse("Missing request_token", status_code=400)

    # Recover original state
    state = pending_auth.pop(f"session_{session}", session)
    auth_info = pending_auth.pop(state, {})

    # Exchange with Zerodha
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    kite_helper.set_access_token(access_token)

    # Generate auth code for Claude
    code = secrets.token_urlsafe(32)
    auth_codes[code] = {
        "access_token": access_token,
        "redirect_uri": auth_info.get("redirect_uri", ""),
        "code_challenge": auth_info.get("code_challenge", ""),
        "code_challenge_method": auth_info.get("code_challenge_method", "plain"),
    }

    redirect_uri = auth_info.get("redirect_uri", "")
    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}&iss={BASE_URL}")

    return PlainTextResponse("Login successful! You can close this tab.")


async def oauth_token(request: Request):
    """Claude exchanges the auth code for a bearer token here."""
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    code = body.get("code")
    if not code or code not in auth_codes:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    code_data = auth_codes.pop(code)
    access_token = code_data["access_token"]

    # Issue bearer token
    bearer = secrets.token_urlsafe(32)
    bearer_tokens[bearer] = access_token
    kite_helper.set_access_token(access_token)

    return JSONResponse({
        "access_token": bearer,
        "token_type": "bearer",
        "expires_in": 86400,
        "scope": "trading",
    })


# ---------- MCP Tools ----------

mcp = FastMCP("zerodha-trading-tools")


@mcp.tool()
def get_historical_candles(tradingsymbol: str, timeframe: str, days_back: int = 60, exchange: str = "NSE") -> str:
    """Get OHLC candle data. timeframe: hourly, daily, 15min, 5min"""
    result = kite_helper.get_historical_candles(tradingsymbol, timeframe, days_back, exchange)
    return json.dumps(result, default=str)


@mcp.tool()
def get_zone_levels(tradingsymbol: str, exchange: str = "NSE") -> str:
    """Get confluence zone: PWH, PWL, PMH, PML, weekly + monthly CPR pivots (S1/R1)."""
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


# ---------- App Assembly ----------

mcp_app = mcp.http_app(path="/mcp")

routes = [
    Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
    Route("/.well-known/oauth-authorization-server", oauth_server_metadata),
    Route("/oauth/register", oauth_register, methods=["POST"]),
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
