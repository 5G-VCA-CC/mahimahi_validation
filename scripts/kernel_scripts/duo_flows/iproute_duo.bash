#!/usr/bin/env bash
# Usage: sudo ./run_duo_qdisc_yaml.bash duo_config.yaml
#
# Dual-flow runner:
#   - Two concurrent iperf3 flows from ns_s -> ns_r
#   - One "l4s" flow (ECT(1) => TOS 0x01)
#   - One "classic" flow (Not-ECT => TOS 0x00)
#   - Uses cset shielding + taskset pinning
#   - Logs:
#       qdisc_<idx>.log   (tc -s qdisc show)
#       ss_<idx>.log      (ss -tin dst <dst_ip>)
#       iperf_l4s_<idx>.log
#       iperf_classic_<idx>.log

set -euo pipefail

CFG=${1:-config.yaml}
if [[ ! -f "$CFG" ]]; then
  echo "Error: config file not found: $CFG"
  exit 1
fi

# --- YAML getter (requires PyYAML) ---
yget() {
  local expr="$1"
  python3 - "$CFG" "$expr" <<'PY'
import sys
cfg_path = sys.argv[1]
expr = sys.argv[2]

try:
  import yaml
except Exception:
  print("PY_YAML_IMPORT_ERROR", file=sys.stderr)
  sys.exit(2)

with open(cfg_path, "r") as f:
  data = yaml.safe_load(f)

cur = data
for part in expr.split("."):
  if part not in cur:
    print("", end="")
    sys.exit(0)
  cur = cur[part]

if cur is None:
  print("", end="")
elif isinstance(cur, bool):
  print("true" if cur else "false", end="")
else:
  print(str(cur), end="")
PY
}

# --- Read config ---
SECS="$(yget runs.secs_per_run)"; SECS="${SECS:-20}"
NUM_RUNS="$(yget runs.num_runs)"; NUM_RUNS="${NUM_RUNS:-1}"
WARMUP_SEC="$(yget runs.warmup_sec)"; WARMUP_SEC="${WARMUP_SEC:-2}"
COOLDOWN_SEC="$(yget runs.cooldown_sec)"; COOLDOWN_SEC="${COOLDOWN_SEC:-3}"

NS_S="$(yget net.ns_s)"; NS_S="${NS_S:-ns_s}"
NS_R="$(yget net.ns_r)"; NS_R="${NS_R:-ns_r}"
VETH_DEV="$(yget net.veth_dev)"; VETH_DEV="${VETH_DEV:-veth-s}"
DST_IP="$(yget net.dst_ip)"; DST_IP="${DST_IP:-172.20.1.2}"
PORT_L4S="$(yget net.port_l4s)"; PORT_L4S="${PORT_L4S:-5202}"
PORT_CLASSIC="$(yget net.port_classic)"; PORT_CLASSIC="${PORT_CLASSIC:-5203}"

RATE="$(yget qdisc.rate)"; RATE="${RATE:-12mbit}"
BURST="$(yget qdisc.burst)"; BURST="${BURST:-15k}"
TARGET="$(yget qdisc.target)"; TARGET="${TARGET:-1ms}"
TUPDATE="$(yget qdisc.tupdate)"; TUPDATE="${TUPDATE:-16ms}"
LIMIT="$(yget qdisc.limit)"; LIMIT="${LIMIT:-15040000}"
ALPHA="$(yget qdisc.alpha)"; ALPHA="${ALPHA:-0.16}"
BETA="$(yget qdisc.beta)"; BETA="${BETA:-3.2}"

# iperf settings
# For true L4S behavior, use TCP Prague or a UDP-CC like SCReAM.
# This script does UDP at fixed rate for both flows (non-reactive), matching your request.
UDP="$(yget iperf.udp)"; UDP="${UDP:-true}"
L4S_BW="$(yget iperf.l4s_bandwidth)"; L4S_BW="${L4S_BW:-12M}"
CLASSIC_BW="$(yget iperf.classic_bandwidth)"; CLASSIC_BW="${CLASSIC_BW:-12M}"
DGRAM_LEN="$(yget iperf.datagram_len)"; DGRAM_LEN="${DGRAM_LEN:-1200}"

LOG_DIR="$(yget logging.dir)"; LOG_DIR="${LOG_DIR:-./tmp}"
SAMPLE_SEC="$(yget logging.sample_sec)"; SAMPLE_SEC="${SAMPLE_SEC:-0.016}"
TIMEOUT_EXTRA="$(yget logging.timeout_extra)"; TIMEOUT_EXTRA="${TIMEOUT_EXTRA:-12}"

HOUSEKEEPING="$(yget cpu.housekeeping)"; HOUSEKEEPING="${HOUSEKEEPING:-0-3}"
EXPERIMENT_CPUS="$(yget cpu.experiment)"; EXPERIMENT_CPUS="${EXPERIMENT_CPUS:-4-7}"
L4S_SENDER_CORE="$(yget cpu.l4s_sender_core)"; L4S_SENDER_CORE="${L4S_SENDER_CORE:-6}"
CLASSIC_SENDER_CORE="$(yget cpu.classic_sender_core)"; CLASSIC_SENDER_CORE="${CLASSIC_SENDER_CORE:-4}"
RECEIVER_CORE="$(yget cpu.receiver_core)"; RECEIVER_CORE="${RECEIVER_CORE:-5}"
LOGGER_CORE="$(yget cpu.logger_core)"; LOGGER_CORE="${LOGGER_CORE:-3}"

mkdir -p "$LOG_DIR"

if (( SECS <= WARMUP_SEC )); then
  echo "Error: secs_per_run must be > warmup_sec (SECS=$SECS, WARMUP_SEC=$WARMUP_SEC)"
  exit 1
fi

LOG_TIME=$(( (SECS - WARMUP_SEC) + TIMEOUT_EXTRA ))

# DS field ECN marking
TOS_L4S="0x01"      # ECT(1)
TOS_CLASSIC="0x00"  # Not-ECT

next_free_idx() {
  local i=0
  while [[ -e "$LOG_DIR/qdisc_${i}.log" || -e "$LOG_DIR/ss_${i}.log" \
        || -e "$LOG_DIR/iperf_l4s_${i}.log" || -e "$LOG_DIR/iperf_classic_${i}.log" ]]; do
    ((i++))
  done
  echo "$i"
}

cleanup_run() {
  local pids=("$@")
  for pid in "${pids[@]}"; do
    [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true
  done
  ip netns exec "$NS_S" pkill -9 iperf3 2>/dev/null || true
  ip netns exec "$NS_R" pkill -9 iperf3 2>/dev/null || true
}

run_pinned_ns() {
  local ns="$1"; shift
  local core="$1"; shift
  ip netns exec "$ns" taskset -c "$core" "$@"
}

run_once_root() {
  local IDX="$1"

  local QDISC_LOG="$LOG_DIR/qdisc_${IDX}.log"
  local SS_LOG="$LOG_DIR/ss_${IDX}.log"
  local IPERF_L4S_LOG="$LOG_DIR/iperf_l4s_${IDX}.log"
  local IPERF_CLASSIC_LOG="$LOG_DIR/iperf_classic_${IDX}.log"

  echo
  echo "[*] === DUO Run #$IDX for ${SECS}s (warmup ${WARMUP_SEC}s not logged) ==="
  echo "[*] qdisc: rate=$RATE burst=$BURST target=$TARGET tupdate=$TUPDATE"
  echo "[*] flows: L4S=$L4S_BW (tos=$TOS_L4S core=$L4S_SENDER_CORE port=$PORT_L4S) | Classic=$CLASSIC_BW (tos=$TOS_CLASSIC core=$CLASSIC_SENDER_CORE port=$PORT_CLASSIC)"
  echo "[*] receiver_core=$RECEIVER_CORE logger_core=$LOGGER_CORE"
  echo "[*] Logs: $QDISC_LOG | $SS_LOG | $IPERF_L4S_LOG | $IPERF_CLASSIC_LOG"

  echo "🔁 Resetting qdisc on $VETH_DEV..."
  ip netns exec "$NS_S" tc qdisc del dev "$VETH_DEV" root 2>/dev/null || true
  ip netns exec "$NS_S" tc qdisc add dev "$VETH_DEV" root handle 1: htb default 10
  ip netns exec "$NS_S" tc class add dev "$VETH_DEV" parent 1: classid 1:10 htb \
    rate "$RATE" ceil "$RATE" burst "$BURST"

  ip netns exec "$NS_S" tc qdisc add dev "$VETH_DEV" parent 1:10 handle 2: dualpi2 \
    target "$TARGET" tupdate "$TUPDATE" limit "$LIMIT" alpha "$ALPHA" beta "$BETA"

  echo "📡 Starting iperf3 servers in $NS_R (core $RECEIVER_CORE)..."
  run_pinned_ns "$NS_R" "$RECEIVER_CORE" iperf3 -s -p "$PORT_L4S" >/dev/null 2>&1 &
  local PID_SRV_L4S=$!
  run_pinned_ns "$NS_R" "$RECEIVER_CORE" iperf3 -s -p "$PORT_CLASSIC" >/dev/null 2>&1 &
  local PID_SRV_CLASSIC=$!

  sleep 1

  echo "🚀 Starting DUO clients in $NS_S for ${SECS}s..."
  if [[ "$UDP" == "true" ]]; then
    run_pinned_ns "$NS_S" "$L4S_SENDER_CORE" iperf3 -c "$DST_IP" -p "$PORT_L4S" -u \
      -b "$L4S_BW" -l "$DGRAM_LEN" -t "$SECS" --tos "$TOS_L4S" --interval 1 \
      >"$IPERF_L4S_LOG" 2>&1 &
    local PID_CLI_L4S=$!

    run_pinned_ns "$NS_S" "$CLASSIC_SENDER_CORE" iperf3 -c "$DST_IP" -p "$PORT_CLASSIC" -u \
      -b "$CLASSIC_BW" -l "$DGRAM_LEN" -t "$SECS" --tos "$TOS_CLASSIC" --interval 1 \
      >"$IPERF_CLASSIC_LOG" 2>&1 &
    local PID_CLI_CLASSIC=$!
  else
    # TCP duo (reactive if you enable prague + ECN in ns_s)
    run_pinned_ns "$NS_S" "$L4S_SENDER_CORE" iperf3 -c "$DST_IP" -p "$PORT_L4S" \
      -t "$SECS" --tos "$TOS_L4S" --interval 1 \
      >"$IPERF_L4S_LOG" 2>&1 &
    local PID_CLI_L4S=$!

    run_pinned_ns "$NS_S" "$CLASSIC_SENDER_CORE" iperf3 -c "$DST_IP" -p "$PORT_CLASSIC" \
      -t "$SECS" --tos "$TOS_CLASSIC" --interval 1 \
      >"$IPERF_CLASSIC_LOG" 2>&1 &
    local PID_CLI_CLASSIC=$!
  fi

  echo "⏳ Warmup ${WARMUP_SEC}s (not logging)..."
  sleep "$WARMUP_SEC"

  echo "📥 Logging ss to $SS_LOG (core $LOGGER_CORE)..."
  (
    taskset -c "$LOGGER_CORE" timeout "${LOG_TIME}s" bash -lc "
      while sleep $SAMPLE_SEC; do
        {
          echo \"------ \$(date) ------\"
          ip netns exec $NS_S ss -tin dst $DST_IP
        } >> \"$SS_LOG\"
      done
    "
  ) & local SS_PID=$!

  echo "📊 Logging qdisc stats to $QDISC_LOG (core $LOGGER_CORE)..."
  (
    taskset -c "$LOGGER_CORE" timeout "${LOG_TIME}s" bash -lc "
      while sleep $SAMPLE_SEC; do
        {
          echo \"------ \$(date) ------\"
          ip netns exec $NS_S tc -s qdisc show dev $VETH_DEV
        } >> \"$QDISC_LOG\"
      done
    "
  ) & local QDISC_PID=$!

  wait "$PID_CLI_L4S" 2>/dev/null || true
  wait "$PID_CLI_CLASSIC" 2>/dev/null || true
  sleep 2

  echo "🛑 Stopping loggers and servers..."
  cleanup_run "$SS_PID" "$QDISC_PID" "$PID_SRV_L4S" "$PID_SRV_CLASSIC"
  echo "✅ Saved: $QDISC_LOG"
}

echo "[*] Using config: $CFG"

echo "[*] Resetting cset..."
sudo cset shield --reset >/dev/null 2>&1 || true

echo "[*] Shielding CPUs: experiment=$EXPERIMENT_CPUS (housekeeping: $HOUSEKEEPING)"
sudo cset shield --cpu="$EXPERIMENT_CPUS" --kthread=on >/dev/null

for ((run=0; run<NUM_RUNS; run++)); do
  IDX="$(next_free_idx)"
  echo ""
  echo "[*] ===== DUO RUN index=$IDX ====="

  sudo cset shield --exec -- bash -lc "
    export SECS='$SECS'
    export WARMUP_SEC='$WARMUP_SEC'
    export LOG_TIME='$LOG_TIME'
    export NS_S='$NS_S'
    export NS_R='$NS_R'
    export VETH_DEV='$VETH_DEV'
    export DST_IP='$DST_IP'
    export PORT_L4S='$PORT_L4S'
    export PORT_CLASSIC='$PORT_CLASSIC'
    export RATE='$RATE'
    export BURST='$BURST'
    export TARGET='$TARGET'
    export TUPDATE='$TUPDATE'
    export LIMIT='$LIMIT'
    export ALPHA='$ALPHA'
    export BETA='$BETA'
    export UDP='$UDP'
    export L4S_BW='$L4S_BW'
    export CLASSIC_BW='$CLASSIC_BW'
    export DGRAM_LEN='$DGRAM_LEN'
    export LOG_DIR='$LOG_DIR'
    export SAMPLE_SEC='$SAMPLE_SEC'
    export TIMEOUT_EXTRA='$TIMEOUT_EXTRA'
    export L4S_SENDER_CORE='$L4S_SENDER_CORE'
    export CLASSIC_SENDER_CORE='$CLASSIC_SENDER_CORE'
    export RECEIVER_CORE='$RECEIVER_CORE'
    export LOGGER_CORE='$LOGGER_CORE'
    $(declare -f next_free_idx cleanup_run run_pinned_ns run_once_root)
    ip netns exec '$NS_S' pkill -9 iperf3 2>/dev/null || true
    ip netns exec '$NS_R' pkill -9 iperf3 2>/dev/null || true
    run_once_root '$IDX'
  "

  echo "[*] Cooling down ${COOLDOWN_SEC}s..."
  sleep "$COOLDOWN_SEC"
done

echo "[*] Releasing cset..."
sudo cset shield --reset >/dev/null 2>&1 || true

echo "[*] All runs complete."
