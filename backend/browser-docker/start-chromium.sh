#!/bin/bash
set -e

PROXY_URL="${PROXY_URL:-}"
START_URL="${START_URL:-https://www.booking.com}"

# Build proxy args only if PROXY_URL is set
PROXY_ARGS=""
if [ -n "$PROXY_URL" ]; then
    PROXY_ARGS="--proxy-server=${PROXY_URL}"
fi

exec /usr/bin/chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-blink-features=AutomationControlled \
    --window-size=1280,900 \
    --start-maximized \
    --ignore-certificate-errors \
    ${PROXY_ARGS} \
    "${START_URL}"
