#!/usr/bin/env bash
# Run this after forcing prague system wide 
# or mahimahi won't use it in its created namespaces

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
    local v
    v="$(yq -r "$key // \"\"" "$CFG" 2>/dev/null || true)"
    [[ "$v" == "null" ]] && v=""
    echo "$v"
  else
    python3 - "$CFG" "$key" <<'PY'
import sys, yaml
cfg, key = sys.argv[1], sys.argv[2]
with open(cfg) as f:
    d = yaml.safe_load(f) or {}
cur = d
ok = True
for p in key.lstrip(".").split("."):
    if isinstance(cur, dict) and p in cur:
        cur = cur[p]
    else:
        ok = False
        break
if not ok or cur is None:
    print("")
else:
    print(cur)
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

# Two delays:
#  - delay_ms: before the bottleneck (propagation baseline)
#  - link_delay_ms: an extra delay INSIDE the mm-link namespace (post-link / second delay stage)
MM_DELAY="$(yaml_get '.mahimahi.delay_ms')"
MM_LINK_DELAY="$(yaml_get '.mahimahi.link_delay_ms')"

QUEUE_TYPE="$(yaml_get '.queue.type')"
Q_PACKETS="$(yaml_get '.queue.packets')"
Q_TARGET="$(yaml_get '.queue.target')"
Q_TUPDATE="$(yaml_get '.queue.tupdate')"
Q_ALPHA="$(yaml_get '.queue.alpha')"
Q_BETA="$(yaml_get '.queue.beta')"

BASE_PORT="$(yaml_get '.flows.base_port')"

CLASSIC_PROTO="$(yaml_get '.flows.classic.proto')"          # if empty => classic disabled
CLASSIC_RATE="$(yaml_get '.flows.classic.rate')"
CLASSIC_PACKET_LEN="$(yaml_get '.flows.classic.packet_len')"
CLASSIC_TOS="$(yaml_get '.flows.classic.tos')"
CLASSIC_CC="$(yaml_get '.flows.classic.cc')"               # optional if proto=tcp

L4S_TOS="$(yaml_get '.flows.l4s.tos')"                     # if empty => l4s disabled
L4S_CC="$(yaml_get '.flows.l4s.cc')"                       # defaulted only if l4s enabled

: "${SECS:=30}"
: "${NUM_RUNS:=1}"
: "${BASE_PORT:=5300}"
: "${MM_DELAY:=0}"
: "${MM_LINK_DELAY:=0}"

QUEUE_ARGS="packets=${Q_PACKETS},target=${Q_TARGET},tupdate=${Q_TUPDATE},alpha=${Q_ALPHA},beta=${Q_BETA}"

echo
echo "================= REGIME SUMMARY ================="
echo "[CFG]           $CFG"
echo "[OUT_DIR]       $OUT_DIR"
echo "[RUN_USER]      $RUN_USER"
echo
echo "[TIME]          secs_per_run=$SECS  num_runs=$NUM_RUNS"
echo
echo "[CPU]           experiment_cpus=$EXPERIMENT_CPUS"
echo "[CPU]           server_core_classic=$SERVER_CORE_CLASSIC  server_core_l4s=$SERVER_CORE_L4S"
echo "[CPU]           mahimahi_core=$MAHIMAHI_CORE"
echo "[CPU]           client_core_classic=$CLIENT_CORE_CLASSIC  client_core_l4s=$CLIENT_CORE_L4S"
echo
echo "[TRACES]        up=$TRACE_UP"
echo "[TRACES]        down=$TRACE_DOWN"
echo
echo "[DELAY]         mahimahi.delay_ms=$MM_DELAY   mahimahi.link_delay_ms=$MM_LINK_DELAY"
echo
echo "[QUEUE]         type=$QUEUE_TYPE"
echo "[QUEUE]         args=$QUEUE_ARGS"
echo
echo "[PORT]          base_port=$BASE_PORT"
echo
echo "[FLOWS]         classic.proto=${CLASSIC_PROTO:-<disabled>}"
if [[ -n "${CLASSIC_PROTO:-}" && "${CLASSIC_PROTO:-}" != "null" ]]; then
  echo "[FLOWS]         classic.tos=$CLASSIC_TOS"
  if [[ "${CLASSIC_PROTO}" == "udp" ]]; then
    echo "[FLOWS]         classic.udp rate=$CLASSIC_RATE  packet_len=$CLASSIC_PACKET_LEN"
  else
    echo "[FLOWS]         classic.tcp cc=${CLASSIC_CC:-<default>}"
  fi
fi
echo
echo "[FLOWS]         l4s.tos=${L4S_TOS:-<disabled>}"
if [[ -n "${L4S_TOS:-}" && "${L4S_TOS:-}" != "null" ]]; then
  echo "[FLOWS]         l4s.cc=${L4S_CC:-prague}"
fi
echo "=================================================="
echo

mkdir -p "$OUT_DIR"
chown -R "$RUN_USER:$RUN_USER" "$OUT_DIR" || true

cleanup_all() {
  pkill -9 -x mm-link 2>/dev/null || true
  pkill -9 -x mm-delay 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true
}

cleanup_mm_netns() {
  command -v ip >/dev/null 2>&1 || return 0
  while read -r ns _; do
    [[ -z "${ns:-}" ]] && continue
    if [[ "$ns" =~ ^mm- ]] || [[ "$ns" =~ ^mahimahi ]] || [[ "$ns" =~ ^mml- ]]; then
      ip netns del "$ns" 2>/dev/null || true
    fi
  done < <(ip netns list 2>/dev/null || true)
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

  local classic_enabled=0
  local l4s_enabled=0

  if [[ -n "${CLASSIC_PROTO:-}" && "${CLASSIC_PROTO:-}" != "null" ]]; then
    classic_enabled=1
  fi
  if [[ -n "${L4S_TOS:-}" && "${L4S_TOS:-}" != "null" ]]; then
    l4s_enabled=1
    : "${L4S_CC:=prague}"   # default only if L4S is enabled
  fi

  if [[ $classic_enabled -eq 0 && $l4s_enabled -eq 0 ]]; then
    echo "[!] No flows enabled. Provide flows.classic and/or flows.l4s in $CFG" >&2
    exit 2
  fi

  local port1=$((BASE_PORT + 2*idx))
  local port2=$((BASE_PORT + 2*idx + 1))
  local out="$OUT_DIR/output_duo_${idx}.txt"

  cleanup_all
  cleanup_mm_netns
  sleep 1

  local srv1="" srv2=""
  if [[ $classic_enabled -eq 1 ]]; then
    taskset -c "$SERVER_CORE_CLASSIC" iperf3 -s -p "$port1" >/dev/null 2>&1 &
    srv1=$!
  fi
  if [[ $l4s_enabled -eq 1 ]]; then
    taskset -c "$SERVER_CORE_L4S" iperf3 -s -p "$port2" >/dev/null 2>&1 &
    srv2=$!
  fi

  sleep 1

  sudo -u "$RUN_USER" \
    taskset -c "$MAHIMAHI_CORE" \
      mm-delay "$MM_DELAY" \
      mm-link \
        --uplink-queue="$QUEUE_TYPE" \
        --uplink-queue-args="$QUEUE_ARGS" \
        "$TRACE_UP" "$TRACE_DOWN" -- \
        mm-delay "$MM_LINK_DELAY" \
        bash -lc "
          set -euo pipefail

          pids=()

          if [[ $classic_enabled -eq 1 ]]; then
            (
              if [[ \"$CLASSIC_PROTO\" == \"udp\" ]]; then
                taskset -c \"$CLIENT_CORE_CLASSIC\" iperf3 -c 10.0.0.1 -p $port1 -u \
                  -b \"$CLASSIC_RATE\" -l \"$CLASSIC_PACKET_LEN\" \
                  -t \"$SECS\" --tos \"$CLASSIC_TOS\"
              elif [[ \"$CLASSIC_PROTO\" == \"tcp\" ]]; then
                if [[ -n \"${CLASSIC_CC:-}\" && \"${CLASSIC_CC:-}\" != \"null\" ]]; then
                  taskset -c \"$CLIENT_CORE_CLASSIC\" iperf3 -c 10.0.0.1 -p $port1 \
                    -C \"$CLASSIC_CC\" \
                    -t \"$SECS\" --tos \"$CLASSIC_TOS\"
                else
                  taskset -c \"$CLIENT_CORE_CLASSIC\" iperf3 -c 10.0.0.1 -p $port1 \
                    -t \"$SECS\" --tos \"$CLASSIC_TOS\"
                fi
              else
                echo \"[!] flows.classic.proto must be udp or tcp (got: $CLASSIC_PROTO)\" >&2
                exit 2
              fi
            ) &
            pids+=(\$!)
          fi

          if [[ $l4s_enabled -eq 1 ]]; then
            (
              taskset -c \"$CLIENT_CORE_L4S\" iperf3 -c 10.0.0.1 -p $port2 \
                -C \"$L4S_CC\" \
                -t \"$SECS\" --tos \"$L4S_TOS\"
            ) &
            pids+=(\$!)
          fi

          for p in \"\${pids[@]}\"; do
            wait \"\$p\"
          done
        " 2>&1 | tee "$out"

  [[ -n "${srv1:-}" ]] && kill "$srv1" 2>/dev/null || true
  [[ -n "${srv2:-}" ]] && kill "$srv2" 2>/dev/null || true

  chown "$RUN_USER:$RUN_USER" "$out" || true
  cleanup_mm_netns
}

cset shield --reset >/dev/null 2>&1 || true
cset shield --cpu="$EXPERIMENT_CPUS" --kthread=on >/dev/null
cset shield --shield >/dev/null

export OUT_DIR SECS NUM_RUNS BASE_PORT
export SERVER_CORE_CLASSIC SERVER_CORE_L4S
export MAHIMAHI_CORE CLIENT_CORE_CLASSIC CLIENT_CORE_L4S
export TRACE_UP TRACE_DOWN
export QUEUE_TYPE QUEUE_ARGS
export MM_DELAY MM_LINK_DELAY

export CLASSIC_PROTO CLASSIC_RATE CLASSIC_PACKET_LEN CLASSIC_TOS CLASSIC_CC
export L4S_TOS L4S_CC
export RUN_USER

export -f next_index
export -f cleanup_all
export -f cleanup_mm_netns
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
cleanup_mm_netns
