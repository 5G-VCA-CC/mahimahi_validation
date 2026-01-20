#!/usr/bin/env bash
# Usage: sudo ./run_many_qdisc_yaml.bash config.yaml
#
# This version uses cset shielding correctly:
#   1) shield experiment CPUs (evict other tasks)
#   2) run the whole experiment inside the shield cpuset via `cset shield --exec`
#   3) pin server/client/loggers to specific cores with taskset (within the shield)

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
PORT="$(yget net.port)"; PORT="${PORT:-5202}"

RATE="$(yget qdisc.rate)"; RATE="${RATE:-12mbit}"
BURST="$(yget qdisc.burst)"; BURST="${BURST:-1k}"
TARGET="$(yget qdisc.target)"; TARGET="${TARGET:-1ms}"
TUPDATE="$(yget qdisc.tupdate)"; TUPDATE="${TUPDATE:-16ms}"
LIMIT="$(yget qdisc.limit)"; LIMIT="${LIMIT:-15040000}"
ALPHA="$(yget qdisc.alpha)"; ALPHA="${ALPHA:-0.16}"
BETA="$(yget qdisc.beta)"; BETA="${BETA:-3.2}"

UDP="$(yget iperf.udp)"; UDP="${UDP:-true}"
BANDWIDTH="$(yget iperf.bandwidth)"; BANDWIDTH="${BANDWIDTH:-12M}"
DGRAM_LEN="$(yget iperf.datagram_len)"; DGRAM_LEN="${DGRAM_LEN:-1200}"
FLOW="$(yget iperf.flow)"; FLOW="${FLOW:-classic}"

LOG_DIR="$(yget logging.dir)"; LOG_DIR="${LOG_DIR:-./tmp}"
SAMPLE_SEC="$(yget logging.sample_sec)"; SAMPLE_SEC="${SAMPLE_SEC:-0.016}"
TIMEOUT_EXTRA="$(yget logging.timeout_extra)"; TIMEOUT_EXTRA="${TIMEOUT_EXTRA:-12}"

HOUSEKEEPING="$(yget cpu.housekeeping)"; HOUSEKEEPING="${HOUSEKEEPING:-0-3}"
EXPERIMENT_CPUS="$(yget cpu.experiment)"; EXPERIMENT_CPUS="${EXPERIMENT_CPUS:-4-7}"
SENDER_CORE="$(yget cpu.sender_core)"; SENDER_CORE="${SENDER_CORE:-4}"
RECEIVER_CORE="$(yget cpu.receiver_core)"; RECEIVER_CORE="${RECEIVER_CORE:-5}"
LOGGER_CORE="$(yget cpu.logger_core)"; LOGGER_CORE="${LOGGER_CORE:-3}"

mkdir -p "$LOG_DIR"

if (( SECS <= WARMUP_SEC )); then
  echo "Error: secs_per_run must be > warmup_sec (SECS=$SECS, WARMUP_SEC=$WARMUP_SEC)"
  exit 1
fi

LOG_TIME=$(( (SECS - WARMUP_SEC) + TIMEOUT_EXTRA ))

# --- Flow marking via ECN bits in DS field (lowest 2 bits) ---
# classic: Not-ECT (00) => 0x00
# l4s:     ECT(1)  (01) => 0x01
case "$FLOW" in
  classic) TOS_HEX="0x00" ;;
  l4s)     TOS_HEX="0x01" ;;
  *)
    echo "Error: iperf.flow must be 'classic' or 'l4s' (got '$FLOW')"
    exit 1
    ;;
esac

next_free_idx() {
  local i=0
  while [[ -e "$LOG_DIR/qdisc_${i}.log" || -e "$LOG_DIR/ss_${i}.log" ]]; do
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

# Runs a command inside a namespace pinned to a core
run_pinned_ns() {
  local ns="$1"; shift
  local core="$1"; shift
  ip netns exec "$ns" taskset -c "$core" "$@"
}

run_once_root() {
  local IDX="$1"

  local QDISC_LOG="$LOG_DIR/qdisc_${IDX}.log"
  local SS_LOG="$LOG_DIR/ss_${IDX}.log"

  echo
  echo "[*] === Run #$IDX for ${SECS}s (warmup ${WARMUP_SEC}s not logged) ==="
  echo "[*] rate=$RATE burst=$BURST target=$TARGET tupdate=$TUPDATE flow=$FLOW tos=$TOS_HEX udp=$UDP"
  echo "[*] sender_core=$SENDER_CORE receiver_core=$RECEIVER_CORE logger_core=$LOGGER_CORE"
  echo "[*] Logs: $QDISC_LOG | $SS_LOG"

  # Sanity: show cpuset allowed list for this runner
  # echo "[*] Allowed CPUs: $(grep Cpus_allowed_list /proc/self/status | awk '{print $2}')"

  echo "🔁 Resetting qdisc on $VETH_DEV..."
  ip netns exec "$NS_S" tc qdisc del dev "$VETH_DEV" root 2>/dev/null || true
  ip netns exec "$NS_S" tc qdisc add dev "$VETH_DEV" root handle 1: htb default 10
  ip netns exec "$NS_S" tc class add dev "$VETH_DEV" parent 1: classid 1:10 htb \
    rate "$RATE" ceil "$RATE" burst "$BURST"

  ip netns exec "$NS_S" tc qdisc add dev "$VETH_DEV" parent 1:10 handle 2: dualpi2 \
    target "$TARGET" tupdate "$TUPDATE" limit "$LIMIT" alpha "$ALPHA" beta "$BETA"

  echo "📡 Starting iperf3 server in $NS_R (core $RECEIVER_CORE)..."
  run_pinned_ns "$NS_R" "$RECEIVER_CORE" iperf3 -s -p "$PORT" >/dev/null 2>&1 &
  local SERVER_PID=$!

  sleep 1

  echo "🚀 Starting iperf3 client in $NS_S (core $SENDER_CORE) for ${SECS}s..."
  if [[ "$UDP" == "true" ]]; then
    run_pinned_ns "$NS_S" "$SENDER_CORE" iperf3 -c "$DST_IP" -p "$PORT" -u \
      -b "$BANDWIDTH" -l "$DGRAM_LEN" -t "$SECS" --tos "$TOS_HEX" >/dev/null 2>&1 &
  else
    run_pinned_ns "$NS_S" "$SENDER_CORE" iperf3 -c "$DST_IP" -p "$PORT" \
      -t "$SECS" --tos "$TOS_HEX" >/dev/null 2>&1 &
  fi
  local CLIENT_PID=$!

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

  wait "$CLIENT_PID" 2>/dev/null || true
  sleep 2

  echo "🛑 Stopping loggers and iperf3..."
  cleanup_run "$SS_PID" "$QDISC_PID" "$SERVER_PID"
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
  echo "[*] ===== RUN index=$IDX ====="

  # Run the whole run_once inside the shield cpuset
  sudo cset shield --exec -- bash -lc "
    export SECS='$SECS'
    export WARMUP_SEC='$WARMUP_SEC'
    export LOG_TIME='$LOG_TIME'
    export NS_S='$NS_S'
    export NS_R='$NS_R'
    export VETH_DEV='$VETH_DEV'
    export DST_IP='$DST_IP'
    export PORT='$PORT'
    export RATE='$RATE'
    export BURST='$BURST'
    export TARGET='$TARGET'
    export TUPDATE='$TUPDATE'
    export LIMIT='$LIMIT'
    export ALPHA='$ALPHA'
    export BETA='$BETA'
    export UDP='$UDP'
    export BANDWIDTH='$BANDWIDTH'
    export DGRAM_LEN='$DGRAM_LEN'
    export FLOW='$FLOW'
    export TOS_HEX='$TOS_HEX'
    export LOG_DIR='$LOG_DIR'
    export SAMPLE_SEC='$SAMPLE_SEC'
    export TIMEOUT_EXTRA='$TIMEOUT_EXTRA'
    export SENDER_CORE='$SENDER_CORE'
    export RECEIVER_CORE='$RECEIVER_CORE'
    export LOGGER_CORE='$LOGGER_CORE'
    $(declare -f next_free_idx cleanup_run run_pinned_ns run_once_root)
    # best-effort cleanup between runs
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
