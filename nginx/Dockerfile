# ── Community Call — Nginx + SSL Container ────────────────────────────────────
# Adds openssl to the official nginx:alpine image so the entrypoint can
# generate a self-signed certificate on first boot.

FROM nginx:alpine

RUN apk add --no-cache openssl

COPY nginx.conf  /etc/nginx/nginx.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
