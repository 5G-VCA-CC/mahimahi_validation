#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "[!] run as root: sudo $0"
  exit 1
fi

# Ensure prague exists
avail="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"
if ! grep -qw prague <<<"$avail"; then
  modprobe tcp_prague 2>/dev/null || true
  avail="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"
fi
if ! grep -qw prague <<<"$avail"; then
  echo "[!] Prague not available. tcp_available_congestion_control: $avail"
  exit 2
fi

echo "[*] Host: setting default congestion control to prague"
sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null
echo "    host tcp_congestion_control = $(sysctl -n net.ipv4.tcp_congestion_control)"

# If any mm-* netns already exist, force prague inside them too
ns_list="$(ip netns list | awk '{print $1}' | grep '^mm-' || true)"
if [[ -n "$ns_list" ]]; then
  echo "[*] Forcing prague inside existing Mahimahi namespaces:"
  while read -r ns; do
    [[ -z "$ns" ]] && continue
    echo "    - $ns"
    ip netns exec "$ns" sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null || true
    cc="$(ip netns exec "$ns" cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || true)"
    echo "      tcp_congestion_control = $cc"
  done <<<"$ns_list"
else
  echo "[*] No existing mm-* namespaces found. New ones created after this will inherit prague."
fi

echo "[+] Done."

cat /proc/sys/net/ipv4/tcp_congestion_control
