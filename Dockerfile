# Multi-stage: build the SPA, then ship a slim Python runtime that serves both
# the API and the built bundle from one process.
FROM node:22-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.13-slim AS runtime

# Never write .pyc / buffer logs: logs must reach the collector immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGBOT_DATA_DIR=/data \
    TRADINGBOT_UI_DIST=/app/ui/dist

WORKDIR /app

# Install dependencies against the lock so the image is reproducible.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . && rm -rf /root/.cache

COPY --from=ui-build /ui/dist ./ui/dist

# Run as a non-root user that owns only the data volume. The application
# filesystem itself can be mounted read-only (see docker-compose.yml).
RUN useradd --system --uid 10001 --create-home --home-dir /home/tradingbot tradingbot \
    && mkdir -p /data \
    && chown -R tradingbot:tradingbot /data \
    && chmod 700 /data
USER tradingbot

EXPOSE 8000

# Liveness for orchestrators that read HEALTHCHECK; /readyz is the gating probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# Exec form so uvicorn is PID 1 and receives SIGTERM directly, letting the
# lifespan shutdown stop bots/streams before exit.
CMD ["uvicorn", "tradingbot.service.main:create_service_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
