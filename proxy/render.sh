#!/usr/bin/env bash
# Render the shared nginx template for one region.
#
#   PUBLIC_IP=<static-ip> ./proxy/render.sh proxy/gcp.env > /etc/nginx/conf.d/registry.conf
#   sudo nginx -t && sudo systemctl reload nginx
#
# Also creates a self-signed cert for the public side if PROXY_TLS_CERT doesn't exist
# (curl it with -k, or trust it explicitly). This is separate from the mTLS certs.
set -euo pipefail
ENV_FILE=${1:?usage: render.sh <region>.env}
DIR=$(cd "$(dirname "$0")" && pwd)

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
: "${PUBLIC_IP:?set PUBLIC_IP=<static IP of this VM> (not stored in the repo)}"

if [ ! -f "$PROXY_TLS_CERT" ]; then
  mkdir -p "$(dirname "$PROXY_TLS_CERT")"
  MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "$PROXY_TLS_KEY" -out "$PROXY_TLS_CERT" -subj "/CN=$REGION_ID" \
    -addext "subjectAltName=IP:$PUBLIC_IP" 2>/dev/null
  echo "generated self-signed proxy cert at $PROXY_TLS_CERT" >&2
fi

VARS='${REGION_ID} ${RATE_LIMIT} ${RATE_BURST} ${PROXY_TLS_CERT} ${PROXY_TLS_KEY}
      ${PAYMENTS_PORT} ${FRAUD_PORT} ${INVENTORY_PORT} ${STANDARD_PORT} ${STANDARD_SERVICE} ${AGENT_PORT}'
envsubst "$VARS" < "$DIR/nginx.conf.template"
