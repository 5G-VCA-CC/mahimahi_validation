#!/usr/bin/env bash
set -euo pipefail

echo "[*] Forcing TCP Prague (hard requirement)"

# --- must be root ---
if [[ $EUID -ne 0 ]]; then
  echo "[!] Must run as root"
  exit 1
fi

# --- enable ECN (required for Prague/L4S) ---
sysctl -w net.ipv4.tcp_ecn=1 >/dev/null

# --- ensure Prague exists ---
AVAIL=$(sysctl -n net.ipv4.tcp_available_congestion_control || true)

if ! grep -qw prague <<<"$AVAIL"; then
  echo "[*] Prague not listed, attempting modprobe tcp_prague"
  modprobe tcp_prague || true
  AVAIL=$(sysctl -n net.ipv4.tcp_available_congestion_control || true)
fi

if ! grep -qw prague <<<"$AVAIL"; then
  echo "[✗] Prague congestion control NOT available"
  echo "    available: $AVAIL"
  exit 2
fi

# --- force system default ---
sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null

# --- verify immediately ---
CC=$(sysctl -n net.ipv4.tcp_congestion_control)
ECN=$(sysctl -n net.ipv4.tcp_ecn)

if [[ "$CC" != "prague" ]]; then
  echo "[✗] tcp_congestion_control is '$CC', expected 'prague'"
  exit 3
fi

if [[ "$ECN" -eq 0 ]]; then
  echo "[✗] tcp_ecn disabled — Prague will not work"
  exit 4
fi

echo "[✓] Kernel ready: tcp_congestion_control=prague tcp_ecn=$ECN"
