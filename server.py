import os
import json
import secrets
import hashlib
import base64
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import RedirectResponse, PlainTextResponse, JSONResponse, Response
from starlette.requests import Request
from starlette.middleware.base import BaseHTTPMiddleware

import kite_helper

API_KEY    = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
BASE_URL   = os.environ.get("BASE_URL", "https://kite-mcp-server-qgsu.onrender.com")
MCP_PATH   = "/mcp"   # must match the Mount below

# In-memory stores
auth_codes        = {}   # code   -> {access_token, redirect_uri, code_challenge, code_challenge_method}
bearer_tokens     = {}   # bearer -> kite_access_token
registered_clients = {}  # client_id -> metadata
pending_auth      = {}   # state  -> {redirect_uri, client_id, code_challenge, code_challenge_method}


# ---------- helpers ----------

def _resource_url():
    return f"{BASE_URL}{MCP_PATH}"

def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    if method == "S256":
        digest = hashlib.sha256(verifier.encode()).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return computed == challenge
    return verifier == challenge   # plain


# ---------- Discovery ----------

async def protected_resource_metadata(request: Request):
    """
    RFC 9728.
    Claude looks here at both:
      /.well-known/oauth-protected-resource          (root path)
      /.well-known/oauth-protected-resource/mcp      (appended MCP path)
    We serve the same response for both.
    """
    return JSONResponse({
        "resource": _resource_url(),
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["trading"],
    })


async def oauth_server_metadata(request: Request):
    """RFC 8414 — Claude discovers our OAuth endpoints."""
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint":         f"{BASE_URL}/oauth/token",
        "registration_endpoint":  f"{BASE_URL}/oauth/register",
        "response_types_supported":          ["code"],
        "grant_types_supported":             ["authorization_code"],
        "code_challenge_methods_supported":  ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


# ---------- Dynamic Client Registration (RFC 7591) ----------

async def oauth_register(request: Request):
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


# ---------- Authorization flow ----------

async def oauth_authorize(request: Request):
    state                = request.query_params.get("state", secrets.token_urlsafe(16))
    redirect_uri         = request.query_params.get("redirect_uri", "")
    client_id            = request.query_params.get("client_id", "")
    code_challenge       = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "plain")

    pending_auth[state] = {
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    # Zerodha appends its own state param; we piggyback our state via the
    # redirect URL that Zerodha will call after login.
    # We store state in a session token and pass it so our callback can recover it.
    session = secrets.token_urlsafe(8)
    pending_auth[f"sess_{session}"] = state
    return RedirectResponse(f"{login_url}&state={session}")


async def kite_callback(request: Request):
    request_token = request.query_params.get("request_token")
    session       = request.query_params.get("state", "")

    if not request_token:
        return PlainTextResponse("Missing request_token", status_code=400)

    state     = pending_auth.pop(f"sess_{session}", session)
    auth_info = pending_auth.pop(state, {})

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    kite_helper.set_access_token(access_token)

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
        return RedirectResponse(
            f"{redirect_uri}{sep}code={code}&state={state}&iss={BASE_URL}"
        )
    return PlainTextResponse("Login successful! You can close this tab.")


async def oauth_token(request: Request):
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    code           = body.get("code", "")
    code_verifier  = body.get("code_verifier", "")

    if not code or code not in auth_codes:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    code_data = auth_codes[code]

    # Verify PKCE
    challenge = code_data.get("code_challenge", "")
    method    = code_data.get("code_challenge_method", "plain")
    if challenge and not _verify_pkce(code_verifier, challenge, method):
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE failed"}, status_code=400)

    auth_codes.pop(code)
    access_token = code_data["access_token"]

    bearer = secrets.token_urlsafe(32)
    bearer_tokens[bearer] = access_token
    kite_helper.set_access_token(access_token)

    return JSONResponse({
        "access_token": bearer,
        "token_type": "bearer",
        "expires_in": 86400,
        "scope": "trading",
    })


# ---------- Auth middleware for MCP routes ----------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Intercepts requests to /mcp.
    - If no/bad token → 401 + WWW-Authenticate header (triggers Claude's OAuth discovery)
    - If valid token → pass through to FastMCP, also refreshing the kite token
    """
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith(MCP_PATH):
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                kite_token = bearer_tokens.get(token)
                if kite_token:
                    kite_helper.set_access_token(kite_token)
                    return await call_next(request)
            # No valid bearer → tell Claude where to auth
            resource_metadata_url = (
                f"{BASE_URL}/.well-known/oauth-protected-resource{MCP_PATH}"
            )
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata_url}"'
                },
            )
        return await call_next(request)


# ---------- MCP tools ----------

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
    """Get option chain (strikes, LTP, OI, bid/ask) for underlying + expiry YYYY-MM-DD."""
    result = kite_helper.get_option_chain(underlying, expiry)
    return json.dumps(result, default=str)


# ---------- App assembly ----------

mcp_app = mcp.http_app(path=MCP_PATH)

routes = [
    # OAuth discovery — serve at both paths Claude checks
    Route("/.well-known/oauth-protected-resource",      protected_resource_metadata),
    Route("/.well-known/oauth-protected-resource/mcp",  protected_resource_metadata),
    Route("/.well-known/oauth-authorization-server",    oauth_server_metadata),
    # OAuth endpoints
    Route("/oauth/register",  oauth_register,  methods=["POST"]),
    Route("/oauth/authorize", oauth_authorize),
    Route("/oauth/token",     oauth_token,     methods=["POST"]),
    # Zerodha callback
    Route("/callback", kite_callback),
    # MCP (protected by middleware)
    Mount("/", app=mcp_app),
]

starlette_app = Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)
starlette_app.add_middleware(BearerAuthMiddleware)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
