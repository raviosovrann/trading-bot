# trading-bot

TradingView webhook → execution bot for BTC futures. See
`BTC-Futures-TradingBot-Design-V1.md` for the design and
`docs/superpowers/plans/` for implementation plans.

## Run locally (M0)

    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env        # then edit WEBHOOK_TOKEN
    export $(grep -v '^#' .env | xargs)
    uvicorn tradingbot.app:create_app --factory --app-dir src --reload

Health check:

    curl localhost:8000/health

Send a test signal (replace TOKEN):

    curl -X POST localhost:8000/webhook \
      -H 'Content-Type: application/json' \
      -d '{"token":"TOKEN","strategy":"btc-futures-v1","action":"buy",
           "symbol":"BTCUSDT","order_type":"market","quantity":0.01,
           "position_side":"long"}'

## Test

    pytest -v

## Expose to TradingView (manual test)

Use a tunnel to get a public HTTPS URL, then set it as the alert webhook URL:

    # e.g. cloudflared tunnel --url http://localhost:8000
    # or:  ngrok http 8000

This step — wiring a real TradingView alert to the tunnel URL and confirming
the round-trip — is a **manual step that a human runs**; it is not performed
as part of automated verification.
