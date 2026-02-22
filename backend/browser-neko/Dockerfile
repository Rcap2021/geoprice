FROM ghcr.io/m1k1o/neko/chromium:2

# Wrapper script that adds --no-sandbox and reads NEKO_PROXY_FLAG / NEKO_START_URL
COPY chromium-wrapper.sh /usr/local/bin/chromium-wrapper.sh
RUN chmod +x /usr/local/bin/chromium-wrapper.sh

# Override the supervisord chromium entry to use our wrapper
COPY chromium.conf /etc/neko/supervisord/chromium.conf

# Defaults (can be overridden at docker run time)
ENV NEKO_PROXY_FLAG=""
ENV NEKO_START_URL="about:blank"
