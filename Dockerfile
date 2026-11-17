# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# tzdata gives zoneinfo the IANA database for quiet-hours handling
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

COPY data ./data
COPY config ./config

RUN useradd --create-home --uid 10001 engine \
 && mkdir -p /var/lib/sms-engine \
 && chown engine:engine /var/lib/sms-engine
USER engine

ENV SMS_ENGINE_DATA_DIR=/app/data \
    SMS_ENGINE_DB=/var/lib/sms-engine/sms_engine.db \
    SMS_ENGINE_LOG_JSON=true

VOLUME ["/var/lib/sms-engine"]
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).status == 200 else 1)"

CMD ["sms-engine", "serve", "--host", "0.0.0.0", "--port", "8099", "--forwarded-allow-ips", "*"]
