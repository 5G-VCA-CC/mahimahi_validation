#!/usr/bin/env bash
set -euo pipefail

CFG="${1:-exp.yaml}"
RUN_USER="${SUDO_USER:-$(id -un)}"

if [[ $EUID -ne 0 ]]; then
  echo "[!] Must run as root (use: sudo $0 $CFG)"
  exit 1
fi

yaml_get() {
  local key="$1"
  if command -v yq >/dev/null 2>&1; then
    yq -r "$key" "$CFG"
  else
    python3 - "$CFG" "$key" <<'PY'
import sys, yaml
cfg, key = sys.argv[1], sys.argv[2]
with open(cfg) as f:
    d = yaml.safe_load(f)
cur = d
for p in key.lstrip(".").split("."):
    cur = cur[p]
print(cur if cur is not None else "")
PY
  fi
}

OUT_DIR="$(realpath -m "$(yaml_get '.output_dir')")"
SECS="$(yaml_get '.secs_per_run')"
NUM_RUNS="$(yaml_get '.num_runs')"

EXPERIMENT_CPUS="$(yaml_get '.experiment_cpus')"

SERVER_CORE_CLASSIC="$(yaml_get '.cores.server_core_classic')"
SERVER_CORE_L4S="$(yaml_get '.cores.server_core_l4s')"
MAHIMAHI_CORE="$(yaml_get '.cores.mahimahi_core')"
CLIENT_CORE_CLASSIC="$(yaml_get '.cores.client_core_classic')"
CLIENT_CORE_L4S="$(yaml_get '.cores.client_core_l4s')"

TRACE_UP="$(yaml_get '.traces.up')"
TRACE_DOWN="$(yaml_get '.traces.down')"

QUEUE_TYPE="$(yaml_get '.queue.type')"
Q_PACKETS="$(yaml_get '.queue.packets')"
Q_TARGET="$(yaml_get '.queue.target')"
Q_TUPDATE="$(yaml_get '.queue.tupdate')"
Q_ALPHA="$(yaml_get '.queue.alpha')"
Q_BETA="$(yaml_get '.queue.beta')"

CLASSIC_RATE="$(yaml_get '.flows.classic.rate')"
CLASSIC_PACKET_LEN="$(yaml_get '.flows.classic.packet_len')"
CLASSIC_TOS="$(yaml_get '.flows.classic.tos')"

L4S_TOS="$(yaml_get '.flows.l4s.tos')"
L4S_CC="$(yaml_get '.flows.l4s.cc')"     # <-- NEW
BASE_PORT="$(yaml_get '.flows.base_port')"

: "${SECS:=30}"
: "${NUM_RUNS:=1}"
: "${BASE_PORT:=5300}"
: "${L4S_CC:=prague}"                     # <-- default

QUEUE_ARGS="packets=${Q_PACKETS},target=${Q_TARGET},tupdate=${Q_TUPDATE},alpha=${Q_ALPHA},beta=${Q_BETA}"

mkdir -p "$OUT_DIR"
chown -R "$RUN_USER:$RUN_USER" "$OUT_DIR" || true

cleanup_all() {
  pkill -9 -x mm-link 2>/dev/null || true
  pkill -9 -x mm-delay 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true
}

next_index() {
  local max=-1
  shopt -s nullglob
  for f in "$OUT_DIR"/output_duo_*.txt; do
    local n="${f##*_}"
    n="${n%.txt}"
    [[ "$n" =~ ^[0-9]+$ ]] && (( n > max )) && max="$n"
  done
  shopt -u nullglob
  echo $((max + 1))
}

run_one() {
  local idx="$1"
  local port1=$((BASE_PORT + 2*idx))
  local port2=$((BASE_PORT + 2*idx + 1))
  local out="$OUT_DIR/output_duo_${idx}.txt"

  cleanup_all
  sleep 1

  # servers (work for both UDP and TCP clients)
  taskset -c "$SERVER_CORE_CLASSIC" iperf3 -s -p "$port1" >/dev/null 2>&1 &
  srv1=$!
  taskset -c "$SERVER_CORE_L4S" iperf3 -s -p "$port2" >/dev/null 2>&1 &
  srv2=$!

  sleep 1

  # Run Mahimahi INSIDE shield. Run as root so we can sysctl *inside* the mm namespace.
  sudo -u "$RUN_USER" \
  taskset -c "$MAHIMAHI_CORE" \
    mm-delay 0 mm-link \
      --uplink-queue="$QUEUE_TYPE" \
      --uplink-queue-args="$QUEUE_ARGS" \
      "$TRACE_UP" "$TRACE_DOWN" -- \
      bash -lc "
        set -euo pipefail

        (
          # classic flow: UDP
          taskset -c \"$CLIENT_CORE_CLASSIC\" iperf3 -c 10.0.0.1 -p $port1 -u \
            -b \"$CLASSIC_RATE\" -l \"$CLASSIC_PACKET_LEN\" \
            -t \"$SECS\" --tos \"$CLASSIC_TOS\"
        ) &
        (
          # l4s flow: TCP + Prague + ECN-capable (ECT(1) via TOS)
          taskset -c \"$CLIENT_CORE_L4S\" iperf3 -c 10.0.0.1 -p $port2 \
            -C \"$L4S_CC\" \
            -t \"$SECS\" --tos \"$L4S_TOS\"
        ) &
        wait
      " 2>&1 | tee "$out"

  kill "$srv1" "$srv2" 2>/dev/null || true
  chown "$RUN_USER:$RUN_USER" "$out" || true
}

cset shield --reset >/dev/null 2>&1 || true
cset shield --cpu="$EXPERIMENT_CPUS" --kthread=on >/dev/null
cset shield --shield >/dev/null

export OUT_DIR SECS NUM_RUNS BASE_PORT
export SERVER_CORE_CLASSIC SERVER_CORE_L4S
export MAHIMAHI_CORE CLIENT_CORE_CLASSIC CLIENT_CORE_L4S
export TRACE_UP TRACE_DOWN
export QUEUE_TYPE QUEUE_ARGS
export CLASSIC_RATE CLASSIC_PACKET_LEN CLASSIC_TOS
export L4S_TOS L4S_CC
export RUN_USER

export -f next_index
export -f cleanup_all
export -f run_one

cset shield --exec -- bash -lc '
  set -euo pipefail
  cat /proc/self/cgroup

  start="$(next_index)"
  for ((i=0; i<NUM_RUNS; i++)); do
    run_one $((start + i))
    sleep 3
  done
'

cset shield --reset >/dev/null 2>&1 || true
cleanup_all
