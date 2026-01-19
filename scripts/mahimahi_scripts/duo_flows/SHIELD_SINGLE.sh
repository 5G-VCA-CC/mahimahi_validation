#!/usr/bin/env bash
set -euo pipefail

CFG="${1:-exp.yaml}"
RUN_USER="${SUDO_USER:-$(id -un)}"

# --- YAML getter (prefers yq, falls back to python+PyYAML) ---
yaml_get() {
  local key="$1"
  if command -v yq >/dev/null 2>&1; then
    yq -r "$key" "$CFG"
  else
    python3 - "$CFG" "$key" <<'PY'
import sys
cfg, key = sys.argv[1], sys.argv[2]
import yaml
with open(cfg, "r") as f:
  data = yaml.safe_load(f)

def get(d, path):
  cur = d
  for part in path.split("."):
    if part == "":
      continue
    cur = cur[part]
  return cur

path = key[1:] if key.startswith(".") else key
val = get(data, path)
if isinstance(val, bool):
  print("true" if val else "false")
elif val is None:
  print("")
else:
  print(val)
PY
  fi
}

# --- read config ---
OUT_DIR="$(yaml_get '.output_dir')"
SECS="$(yaml_get '.secs_per_run')"
NUM_RUNS="$(yaml_get '.num_runs')"

HOUSEKEEPING="$(yaml_get '.housekeeping')"
EXPERIMENT_CPUS="$(yaml_get '.experiment_cpus')"

MAHI_CORE="$(yaml_get '.cores.mahi_core')"
SERVER_CORE="$(yaml_get '.cores.server_core')"
CLIENT_CORE="$(yaml_get '.cores.client_core')"

TRACE_UP="$(yaml_get '.traces.up')"
TRACE_DOWN="$(yaml_get '.traces.down')"

QUEUE_TYPE="$(yaml_get '.queue.type')"
Q_PACKETS="$(yaml_get '.queue.packets')"
Q_TARGET="$(yaml_get '.queue.target')"
Q_TUPDATE="$(yaml_get '.queue.tupdate')"
Q_ALPHA="$(yaml_get '.queue.alpha')"
Q_BETA="$(yaml_get '.queue.beta')"

FLOW_MODE="$(yaml_get '.flow.mode')"
L4S_TOS="$(yaml_get '.flow.l4s_tos')"
RATE="$(yaml_get '.flow.rate')"
PACKET_LEN="$(yaml_get '.flow.packet_len')"
BASE_PORT="$(yaml_get '.flow.base_port')"

# defaults
: "${L4S_TOS:=1}"
: "${RATE:=12M}"
: "${PACKET_LEN:=1200}"
: "${BASE_PORT:=5300}"
mkdir -p "$OUT_DIR"

QUEUE_ARGS="packets=${Q_PACKETS},target=${Q_TARGET},tupdate=${Q_TUPDATE},alpha=${Q_ALPHA},beta=${Q_BETA}"

# --- pick next index automatically based on existing output files ---
next_index() {
  local pattern="$1"
  local max=-1
  shopt -s nullglob
  for f in "$OUT_DIR"/$pattern; do
    local base="${f##*/}"
    local num="${base##*_}"
    num="${num%.txt}"
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    (( num > max )) && max="$num"
  done
  shopt -u nullglob
  echo $((max + 1))
}

# map mode -> tos + filename prefix
FLOW_PREFIX=""
TOS_VALUE="0"
if [[ "$FLOW_MODE" == "l4s" ]]; then
  FLOW_PREFIX="l4s"
  TOS_VALUE="$L4S_TOS"
else
  FLOW_PREFIX="classic"
  TOS_VALUE="0"
fi

START_INDEX="$(next_index "output_${FLOW_PREFIX}_*.txt")"

# --- experiment function (RUNS AS ROOT INSIDE SHIELD) ---
run_mahi_single_root() {
  local SECS="$1"
  local IDX="$2"

  local PORT=$((BASE_PORT + IDX))
  local OUT_FILE="${OUT_DIR}/output_${FLOW_PREFIX}_${IDX}.txt"
  local FLOW_LOG="${OUT_DIR}/iperf_${FLOW_PREFIX}_${IDX}.txt"

  echo "[*] Cleaning up Mahimahi + iperf (root)..."

  # IMPORTANT: do NOT use pkill -f here (it can match the current bash -lc argv and kill itself)
  pkill -9 -x mm-link 2>/dev/null || true
  pkill -9 -x mm-delay 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true

  for ns in $(ip netns list | awk '{print $1}' | grep '^mm-' || true); do
    ip netns del "$ns" 2>/dev/null || true
  done

  sleep 1

  echo "[*] Starting iperf3 server on port $PORT (core $SERVER_CORE)..."
  taskset -c "$SERVER_CORE" iperf3 -s -p "$PORT" >/dev/null 2>&1 &
  local PID_SRV=$!

  sleep 1

  echo "[*] Running Mahimahi on core $MAHI_CORE as user=$RUN_USER (mode=$FLOW_MODE idx=$IDX tos=$TOS_VALUE)..."

  local USER_HOME
  USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  : "${USER_HOME:=/home/$RUN_USER}"

  # Mahimahi must run NON-ROOT
  sudo -u "$RUN_USER" env \
    HOME="$USER_HOME" USER="$RUN_USER" LOGNAME="$RUN_USER" PATH="$PATH" \
    bash -lc "
      set -euo pipefail
      taskset -c '$MAHI_CORE' mm-delay 0 mm-link --meter-all \
        --uplink-queue='$QUEUE_TYPE' \
        --uplink-queue-args='$QUEUE_ARGS' \
        '$TRACE_UP' '$TRACE_DOWN' -- bash -c \"
          echo '[+] Flow: mode=$FLOW_MODE port=$PORT tos=$TOS_VALUE'
          taskset -c $CLIENT_CORE iperf3 -c 10.0.0.1 -p $PORT -u \
            -b $RATE -l $PACKET_LEN -t $SECS --tos $TOS_VALUE --interval 1 \
            2>&1 | tee '$FLOW_LOG'
        \" | tee '$OUT_FILE'
    "

  kill "$PID_SRV" 2>/dev/null || true
  echo "[*] Done. Logs in $OUT_DIR"
}

# --- CPU shielding setup (requires root) ---
echo "[*] Using config: $CFG"
echo "[*] Resetting cset..."
sudo cset shield --reset || true

echo "[*] Shielding CPUs: $EXPERIMENT_CPUS (housekeeping: $HOUSEKEEPING)"
sudo cset shield --cpu="$EXPERIMENT_CPUS" --kthread=on

# --- run loop ---
for r in $(seq 0 $((NUM_RUNS - 1))); do
  idx=$((START_INDEX + r))
  echo ""
  echo "[*] ===== RUN index=$idx ====="

  sudo cset shield --exec -- bash -lc "
    export RUN_USER='$RUN_USER'
    export MAHI_CORE='$MAHI_CORE'
    export SERVER_CORE='$SERVER_CORE'
    export CLIENT_CORE='$CLIENT_CORE'
    export FLOW_MODE='$FLOW_MODE'
    export TOS_VALUE='$TOS_VALUE'
    export QUEUE_TYPE='$QUEUE_TYPE'
    export QUEUE_ARGS='$QUEUE_ARGS'
    export TRACE_UP='$TRACE_UP'
    export TRACE_DOWN='$TRACE_DOWN'
    export OUT_DIR='$OUT_DIR'
    export RATE='$RATE'
    export PACKET_LEN='$PACKET_LEN'
    export BASE_PORT='$BASE_PORT'
    export FLOW_PREFIX='$FLOW_PREFIX'
    $(declare -f run_mahi_single_root)
    run_mahi_single_root '$SECS' '$idx'
  "

  sleep 3
done

echo "[*] Releasing cset..."
sudo cset shield --reset
