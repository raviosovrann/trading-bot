import logging

from fastapi import FastAPI, HTTPException, Request

from .auth import ip_allowed, is_authorized
from .config import Config, load_config
from .parser import SignalParseError, parse_signal

logger = logging.getLogger("tradingbot")


def _configure_logging() -> None:
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False


def create_app(config: Config | None = None) -> FastAPI:
    config = load_config() if config is None else config
    _configure_logging()
    app = FastAPI(title="TradingBot Webhook")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "venue": config.venue}

    @app.post("/webhook")
    async def webhook(request: Request) -> dict:
        client_ip = request.client.host if request.client else ""
        if not ip_allowed(client_ip, config.allowed_ips):
            logger.warning("Rejected webhook from disallowed IP: %s", client_ip)
            raise HTTPException(status_code=403, detail="IP not allowed")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        token = payload.get("token") if isinstance(payload, dict) else None
        if not is_authorized(token, config.webhook_token):
            logger.warning("Rejected webhook: bad/missing token from %s", client_ip)
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            signal = parse_signal(payload)
        except SignalParseError:
            logger.warning("Rejected webhook: invalid signal payload from %s", client_ip)
            raise HTTPException(status_code=422, detail="Invalid signal")

        # M0: log only. M1 swaps this line for router.handle(signal).
        logger.info(
            "Signal received: action=%s symbol=%s qty=%s side=%s strategy=%s",
            signal.action.value,
            signal.symbol,
            signal.quantity,
            signal.position_side.value,
            signal.strategy,
        )
        return {
            "status": "received",
            "action": signal.action.value,
            "symbol": signal.symbol,
        }

    return app
