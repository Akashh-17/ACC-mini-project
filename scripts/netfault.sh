#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:?usage: netfault.sh ACTION PEER_IP [DELAY_MS]}
PEER_IP=${2:?usage: netfault.sh ACTION PEER_IP [DELAY_MS]}
IFACE=${IFACE:-$(ip route get "$PEER_IP" | awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')}
PORT=${REPLICATION_PORT:-9443}

# Stable per-peer filter handles let a second delay command preserve the first.
# These experiments use public IPv4 peers, matching protocol ip below.
if [[ ! "$PEER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "PEER_IP must be an IPv4 address" >&2
  exit 2
fi
IFS=. read -r octet1 octet2 octet3 octet4 <<< "$PEER_IP"
for octet in "$octet1" "$octet2" "$octet3" "$octet4"; do
  if (( 10#$octet > 255 )); then echo "invalid IPv4 address" >&2; exit 2; fi
done
FILTER_HANDLE=$(printf '0x%08x' "$(( (10#$octet1 << 24) | (10#$octet2 << 16) | (10#$octet3 << 8) | 10#$octet4 ))")

has_delay_root() {
  tc qdisc show dev "$IFACE" | grep -q 'qdisc prio 1: root'
}

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
    if [[ ! "$DELAY_MS" =~ ^[0-9]+$ ]]; then echo "delay must be milliseconds" >&2; exit 2; fi
    # Replacing root unconditionally discarded all previously added peers.
    if ! has_delay_root; then tc qdisc replace dev "$IFACE" root handle 1: prio; fi
    tc qdisc replace dev "$IFACE" parent 1:3 handle 30: netem delay "${DELAY_MS}ms"
    tc filter replace dev "$IFACE" protocol ip parent 1:0 pref 3 handle "$FILTER_HANDLE" flower \
      ip_proto tcp dst_ip "$PEER_IP"/32 dst_port "$PORT" classid 1:3
    ;;
  clear)
    iptables -D OUTPUT -p tcp -d "$PEER_IP" --dport "$PORT" -j DROP 2>/dev/null || true
    iptables -D OUTPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP 2>/dev/null || true
    iptables -D INPUT -p tcp -s "$PEER_IP" --dport "$PORT" -j DROP 2>/dev/null || true
    iptables -D INPUT -p tcp -s "$PEER_IP" --sport "$PORT" -j DROP 2>/dev/null || true
    # Clear only this peer's delay, leaving the other peer delayed until cleared.
    if has_delay_root; then
      tc filter del dev "$IFACE" protocol ip parent 1:0 pref 3 handle "$FILTER_HANDLE" flower 2>/dev/null || true
      remaining_filters=$(tc filter show dev "$IFACE" parent 1:0)
      if [[ -z "$remaining_filters" ]]; then tc qdisc del dev "$IFACE" root; fi
    fi
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 2
    ;;
esac
