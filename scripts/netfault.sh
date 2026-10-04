#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:?usage: $0 {partition|delay|clear} PEER_IP [DELAY_MS]}
PEER_IP=${2:?usage: $0 {partition|delay|clear} PEER_IP [DELAY_MS]}
IFACE=${IFACE:-$(ip route get "$PEER_IP" | awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')}
PORT=${REPLICATION_PORT:-9443}

case "$ACTION" in
  partition)
    # Drop both directions of the TCP conversation: requests and responses.
    iptables -C OUTPUT -p tcp -d "$PEER_IP" --dport "$PORT" -j DROP 2>/dev/null || \
      iptables -A OUTPUT -p tcp -d "$PEER_IP" --dport "$PORT" -j DROP
    iptables -C OUTPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP 2>/dev/null || \
      iptables -A OUTPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP
    iptables -C INPUT -p tcp -s "$PEER_IP" --dport "$PORT" -j DROP 2>/dev/null || \
      iptables -A INPUT -p tcp -s "$PEER_IP" --dport "$PORT" -j DROP
    iptables -C INPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP 2>/dev/null || \
      iptables -A INPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP
    ;;
  delay)
    DELAY_MS=${3:?delay requires DELAY_MS}
    tc qdisc replace dev "$IFACE" root handle 1: prio
    tc qdisc replace dev "$IFACE" parent 1:3 handle 30: netem delay "${DELAY_MS}ms"
    tc filter replace dev "$IFACE" protocol ip parent 1:0 prio 3 u32 \
      match ip dst "$PEER_IP"/32 match ip dport "$PORT" 0xffff flowid 1:3
    ;;
  clear)
    iptables -D OUTPUT -p tcp -d "$PEER_IP" --dport "$PORT" -j DROP 2>/dev/null || true
    iptables -D OUTPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP 2>/dev/null || true
    iptables -D INPUT -p tcp -s "$PEER_IP" --dport "$PORT" -j DROP 2>/dev/null || true
    iptables -D INPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP 2>/dev/null || true
    tc qdisc del dev "$IFACE" root 2>/dev/null || true
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 2
    ;;
esac