# Session-Bound API Key Proxy for DeepSeek Harness
# Self-contained image: FastAPI + httpx + SQLite (WAL).
# Persist the SQLite DB on a mounted disk at /var/data (see render.yaml).
FROM python:3.14-slim

# Non-root user (runs as UID 10001 for better hygiene on Render).
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and the key-pool seed file.
COPY app/ ./app/
COPY keys.json ./keys.json

# Every proxy instance binds ONE DeepSeek key per session id. The key pool is
# seeded from KEYS_FILE only on a fresh (empty) DB, so write the real keys
# into keys.json BEFORE first boot.
ENV DB_PATH=/app/data/proxy.db
ENV KEYS_FILE=/app/keys.json

# App data directory (no persistent disk on free tier; container-writable).
RUN mkdir -p /app/data && chown -R app:app /app/data

# The app binds to this port; Render keeps $PORT in sync (default 8000).
EXPOSE 8000

# Use a shell form so Render's injected $PORT wins, falling back to 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]

# Healthcheck must NOT hit /admin/* (those require ADMIN_TOKEN); /healthz is
# the unauthenticated liveness endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

USER app
