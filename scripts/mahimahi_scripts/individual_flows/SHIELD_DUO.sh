#!/usr/bin/env bash
set -euo pipefail

CFG="${1:-exp.yaml}"

# flow mode must be duo for this script (or you can ignore and always run duo)
# Uses SAME YAML schema as your working single script, plus:
#   cores:
#     l4s_client_core: 6
#     classic_client_core: 4
#   flow:
#     l4s_tos: 1
#     classic_tos: 0          # 0 for non-ECN classic; set 2 for ECT(0) if you want
#
# Example:
#   sudo ./SHIELD_DUO.sh exp.yaml

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
L4S_CLIENT_CORE="$(yaml_get '.cores.l4s_client_core')"
CLASSIC_CLIENT_CORE="$(yaml_get '.cores.classic_client_core')"

TRACE_UP="$(yaml_get '.traces.up')"
TRACE_DOWN="$(yaml_get '.traces.down')"

QUEUE_TYPE="$(yaml_get '.queue.type')"
Q_PACKETS="$(yaml_get '.queue.packets')"
Q_TARGET="$(yaml_get '.queue.target')"
Q_TUPDATE="$(yaml_get '.queue.tupdate')"
Q_ALPHA="$(yaml_get '.queue.alpha')"
Q_BETA="$(yaml_get '.queue.beta')"

RATE="$(yaml_get '.flow.rate')"
PACKET_LEN="$(yaml_get '.flow.packet_len')"
BASE_PORT="$(yaml_get '.flow.base_port')"

L4S_TOS="$(yaml_get '.flow.l4s_tos')"
CLASSIC_TOS="$(yaml_get '.flow.classic_tos')"

# defaults
: "${RATE:=12M}"
: "${PACKET_LEN:=1200}"
: "${BASE_PORT:=5300}"
: "${L4S_TOS:=1}"
: "${CLASSIC_TOS:=0}"

mkdir -p "$OUT_DIR"

QUEUE_ARGS="packets=${Q_PACKETS},target=${Q_TARGET},tupdate=${Q_TUPDATE},alpha=${Q_ALPHA},beta=${Q_BETA}"

# --- pick next index automatically based on existing duo output files ---
next_index() {
  local pattern="$1"  # e.g. output_duo_*.txt
  local max=-1
  shopt -s nullglob
  for f in "$OUT_DIR"/$pattern; do
    local base="${f##*/}"      # output_duo_12.txt
    local num="${base##*_}"    # 12.txt
    num="${num%.txt}"          # 12
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    (( num > max )) && max="$num"
  done
  shopt -u nullglob
  echo $((max + 1))
}

START_INDEX="$(next_index "output_duo_*.txt")"

# --- experiment function (RUNS AS ROOT INSIDE SHIELD) ---
run_mahi_duo_root() {
  local SECS="$1"
  local IDX="$2"

  local PORT_L4S=$((BASE_PORT + 2*IDX))
  local PORT_CLASSIC=$((PORT_L4S + 1))

  local OUT_FILE="${OUT_DIR}/output_duo_${IDX}.txt"
  local FLOW_L4S="${OUT_DIR}/iperf_l4s_${IDX}.txt"
  local FLOW_CLASSIC="${OUT_DIR}/iperf_classic_${IDX}.txt"

  echo "[*] Cleaning up Mahimahi + iperf (root)..."
  # IMPORTANT: do NOT use pkill -f (can kill this runner because argv contains strings)
  pkill -9 -x mm-link 2>/dev/null || true
  pkill -9 -x mm-delay 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true

  for ns in $(ip netns list | awk '{print $1}' | grep '^mm-' || true); do
    ip netns del "$ns" 2>/dev/null || true
  done

  sleep 1

  echo "[*] Starting iperf3 servers (core $SERVER_CORE)..."
  taskset -c "$SERVER_CORE" iperf3 -s -p "$PORT_L4S" >/dev/null 2>&1 &
  local PID_L4S_SRV=$!
  taskset -c "$SERVER_CORE" iperf3 -s -p "$PORT_CLASSIC" >/dev/null 2>&1 &
  local PID_CLASSIC_SRV=$!

  sleep 1

  echo "[*] Running Mahimahi on core $MAHI_CORE as user=$RUN_USER (IDX=$IDX)..."

  local USER_HOME
  USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  : "${USER_HOME:=/home/$RUN_USER}"

  # Mahimahi must run NON-ROOT; inside it we run two iperf clients in parallel.
  sudo -u "$RUN_USER" env \
    HOME="$USER_HOME" USER="$RUN_USER" LOGNAME="$RUN_USER" PATH="$PATH" \
    bash -lc "
      set -euo pipefail
      taskset -c '$MAHI_CORE' mm-delay 0 mm-link --meter-all \
        --uplink-queue='$QUEUE_TYPE' \
        --uplink-queue-args='$QUEUE_ARGS' \
        '$TRACE_UP' '$TRACE_DOWN' -- bash -c \"
          echo '[+] L4S flow: port=$PORT_L4S tos=$L4S_TOS core=$L4S_CLIENT_CORE'
          taskset -c $L4S_CLIENT_CORE iperf3 -c 10.0.0.1 -p $PORT_L4S -u \
            -b $RATE -l $PACKET_LEN -t $SECS --tos $L4S_TOS --interval 1 \
            2>&1 | tee '$FLOW_L4S' &

          echo '[+] Classic flow: port=$PORT_CLASSIC tos=$CLASSIC_TOS core=$CLASSIC_CLIENT_CORE'
          taskset -c $CLASSIC_CLIENT_CORE iperf3 -c 10.0.0.1 -p $PORT_CLASSIC -u \
            -b $RATE -l $PACKET_LEN -t $SECS --tos $CLASSIC_TOS --interval 1 \
            2>&1 | tee '$FLOW_CLASSIC' &

          wait
        \" | tee '$OUT_FILE'
    "

  echo "[*] Stopping servers..."
  kill "$PID_L4S_SRV" "$PID_CLASSIC_SRV" 2>/dev/null || true

  echo "[*] Done. Logs in $OUT_DIR"
}

# --- CPU shielding setup ---
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
    export L4S_CLIENT_CORE='$L4S_CLIENT_CORE'
    export CLASSIC_CLIENT_CORE='$CLASSIC_CLIENT_CORE'
    export QUEUE_TYPE='$QUEUE_TYPE'
    export QUEUE_ARGS='$QUEUE_ARGS'
    export TRACE_UP='$TRACE_UP'
    export TRACE_DOWN='$TRACE_DOWN'
    export OUT_DIR='$OUT_DIR'
    export RATE='$RATE'
    export PACKET_LEN='$PACKET_LEN'
    export BASE_PORT='$BASE_PORT'
    export L4S_TOS='$L4S_TOS'
    export CLASSIC_TOS='$CLASSIC_TOS'
    $(declare -f run_mahi_duo_root)
    run_mahi_duo_root '$SECS' '$idx'
  "

  sleep 3
done

echo "[*] Releasing cset..."
sudo cset shield --reset
