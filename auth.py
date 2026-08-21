"""
Daily login helper.

Kite Connect access tokens expire every day (security requirement from
Zerodha/SEBI, not something we can bypass). Each trading day you:

  1. Visit https://<your-render-url>/login  -> redirects to Zerodha login
  2. Log in with your Zerodha credentials + 2FA
  3. Zerodha redirects back to /callback with a request_token
  4. This exchanges it for a fresh access_token and stores it in memory

After that, the MCP server tools work normally until the token expires
again (usually ~6am the next day).
"""

import os
from starlette.responses import RedirectResponse, PlainTextResponse
from kiteconnect import KiteConnect
import kite_helper

API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")


async def login(request):
    kite = KiteConnect(api_key=API_KEY)
    return RedirectResponse(kite.login_url())


async def callback(request):
    request_token = request.query_params.get("request_token")
    if not request_token:
        return PlainTextResponse("Missing request_token", status_code=400)

    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]

    kite_helper.set_access_token(access_token)

    return PlainTextResponse(
        "Login successful. Access token is active for today. "
        "You can close this tab and use the MCP tools now."
    )
