# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile some wheels (motor/pymongo C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Non-root user for security
RUN useradd --create-home --shell /bin/bash botuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source (path is relative to the build context — repo root)
COPY ./bot ./bot
COPY requirements.txt .

# Temp directory writable by botuser
RUN mkdir -p /app/temp && chown botuser:botuser /app/temp

USER botuser

# Railway sets PORT but the bot uses polling, so no port binding needed.
# If you switch to webhooks, EXPOSE 8443 here.

# Fail fast if BOT_TOKEN or MONGO_URI are missing (validated in config.py)
CMD ["python", "-m", "bot.main"]
