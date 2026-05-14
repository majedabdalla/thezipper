# ── Stage 1: Build telegram-bot-api from source ──────────────────────────────
FROM debian:bookworm-slim AS tgapi-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    make git zlib1g-dev libssl-dev gperf cmake g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --recursive --depth=1 https://github.com/tdlib/telegram-bot-api.git /src

RUN mkdir -p /src/build && cd /src/build && \
    cmake -DCMAKE_BUILD_TYPE=Release \
          -DCMAKE_INSTALL_PREFIX=/usr/local \
          -DOPENSSL_ROOT_DIR=/usr \
          .. && \
    cmake --build . --target install -j$(nproc)

# ── Stage 2: Python deps builder ─────────────────────────────────────────────
FROM python:3.11-slim AS py-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 3: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

COPY --from=tgapi-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libssl3 \
    zlib1g \
    libstdc++6 \
    libgcc-s1 \
    libc6 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash botuser

WORKDIR /app

COPY --from=py-builder /install /usr/local
COPY ./bot ./bot
COPY requirements.txt .
COPY start.sh .

RUN mkdir -p /app/temp /app/tg_data && chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1

CMD ["/bin/bash", "/app/start.sh"]







