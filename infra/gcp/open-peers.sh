#!/usr/bin/env bash
# Phase 4.1 — open the replication port to the two peer agents only, and the
# public proxy (443) to whoever needs to curl it (default: peers + Teammate 4).
#
#   AWS_IP=13.x.x.x AZURE_IP=20.x.x.x T4_IP=198.51.100.9 bash infra/gcp/open-peers.sh
#
# Re-run after any IP changes; rules are updated in place.
set -euo pipefail
AWS_IP=${AWS_IP:?}
AZURE_IP=${AZURE_IP:?}
NETWORK=registry-net
TAG=registry-agent
PEERS="$AWS_IP/32,$AZURE_IP/32"
PROXY_SOURCES=${PROXY_SOURCES:-$PEERS${T4_IP:+,$T4_IP/32}}

upsert() {  # name rules sources
  if gcloud compute firewall-rules describe "$1" >/dev/null 2>&1; then
    gcloud compute firewall-rules update "$1" --rules="$2" --source-ranges="$3"
  else
    gcloud compute firewall-rules create "$1" --network="$NETWORK" --direction=INGRESS \
      --action=ALLOW --rules="$2" --source-ranges="$3" --target-tags="$TAG"
  fi
}

upsert registry-allow-replication tcp:9443 "$PEERS"
upsert registry-allow-proxy tcp:443,icmp "$PROXY_SOURCES"
gcloud compute firewall-rules list --filter="network:$NETWORK" \
  --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list())"
