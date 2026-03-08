# ── Community Call — App Container ───────────────────────────────────────────
# Multi-stage build keeps the final image lean.
# The app runs on internal port 8000; nginx handles external HTTPS on 443.

FROM python:3.12-slim AS base

# System deps needed by pyserial (serial port access) and healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies ───────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application source ───────────────────────────────────────────────────
COPY . .

# Persistent data directory — mount a Docker volume here to preserve the DB
# across container restarts / image upgrades.
RUN mkdir -p /data

# ── Runtime config ────────────────────────────────────────────────────────────
ENV DB_PATH=/data/nurse_call.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Health endpoint is answered quickly by the lifespan-started app
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

# Single worker — required because session tokens and WS state live in-process
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
