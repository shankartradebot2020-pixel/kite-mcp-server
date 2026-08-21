# Zerodha Kite MCP Server

Connects Claude to your Zerodha account for historical candles, weekly/monthly
zone levels (PWH/PWL/PMH/PML + CPR pivots), EMA crossover detection, and
option chain data.

## 1. Get your Kite Connect app credentials

1. Go to https://developers.kite.trade/apps and create a new app (you already
   have the paid ₹500/month plan, so this should already be enabled).
2. Set the **Redirect URL** to: `https://<your-render-app-name>.onrender.com/callback`
   (you'll get the exact `.onrender.com` URL in step 3 below — come back and
   update this after deploying once).
3. Note down your **API key** and **API secret**.

## 2. Deploy to Render (free tier)

1. Create a free account at https://render.com
2. Push this folder to a new GitHub repo (or use Render's "Deploy from
   local files" option if available).
3. In Render: **New > Web Service**, connect the repo.
4. Build command: `pip install -r requirements.txt`
5. Start command: `python server.py`
6. Add environment variables (Render dashboard > Environment):
   - `KITE_API_KEY`
   - `KITE_API_SECRET`
   - (leave `KITE_ACCESS_TOKEN` unset — it's set daily via login, not stored)
7. Deploy. Render will give you a URL like `https://kite-mcp-yourname.onrender.com`.
8. Go back to your Kite Connect app settings and set the Redirect URL to
   `https://kite-mcp-yourname.onrender.com/callback`.

**Free tier note:** Render's free web services sleep after ~15 minutes of no
traffic and take ~30-60 seconds to wake up on the next request. Fine for this
use case — you're checking setups manually, not running high-frequency stuff.

## 3. Daily login (required — Kite tokens expire every day)

Each trading day, before asking Claude to check a setup:

1. Open `https://kite-mcp-yourname.onrender.com/login` in your browser
2. Log in with your Zerodha credentials + 2FA (same as the Kite app)
3. You'll see "Login successful" — that's it, you're done for the day

This is a Zerodha/SEBI security requirement, not something this server can
skip — there's no way to make the token last longer than a day.

## 4. Connect it to Claude

1. In Claude, go to **Settings > Connectors > Add custom connector**
2. Name: `Zerodha Trading Tools`
3. URL: `https://kite-mcp-yourname.onrender.com/mcp`
4. No OAuth needed here (auth happens via the daily login step above, which
   sets the token inside the running server) — save.
5. Enable the connector in your chat.

## Tools this exposes to Claude

- `get_historical_candles` — OHLC data (hourly/daily/etc.)
- `get_zone_levels` — PWH/PWL/PMH/PML + weekly & monthly CPR pivots (S1/R1/etc.)
- `detect_ema_crossovers` — 5/20 EMA bullish/bearish crossovers on hourly candles
- `get_option_chain` — strikes, LTP, OI, bid/ask for an underlying + expiry

## Known limitations

- Free Render tier sleeps when idle — first request after a while is slow.
- Access token must be refreshed every trading day via `/login`.
- `get_option_chain` doesn't return IV/Greeks directly (Kite's quote API
  doesn't provide these) — Claude can estimate IV context using price action
  and ATM straddle premium once you have chain data; a dedicated IV feed
  (e.g. Sensibull, NSE option chain) would be more precise if you want that
  later.
