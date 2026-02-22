#!/bin/bash
# Wraps /usr/bin/chromium so neko's supervisord can inject custom flags.
# Environment variables read:
#   NEKO_PROXY_FLAG  - e.g. "--proxy-server=http://127.0.0.1:8790"  (optional)
#   NEKO_START_URL   - URL to open on launch                        (optional, default: about:blank)

PROXY_FLAG="${NEKO_PROXY_FLAG:-}"
START_URL="${NEKO_START_URL:-about:blank}"

ARGS=(
    --window-position=0,0
    --display="$DISPLAY"
    --user-data-dir=/home/neko/.config/chromium
    --no-first-run
    --start-maximized
    --bwsi
    --force-dark-mode
    --disable-file-system
    --disable-gpu
    --disable-software-rasterizer
    --disable-dev-shm-usage
    # Required on older kernels / rootless containers
    --no-sandbox
    --disable-setuid-sandbox
)

if [ -n "$PROXY_FLAG" ]; then
    ARGS+=("$PROXY_FLAG")
fi

ARGS+=("$START_URL")

exec /usr/bin/chromium "${ARGS[@]}"
