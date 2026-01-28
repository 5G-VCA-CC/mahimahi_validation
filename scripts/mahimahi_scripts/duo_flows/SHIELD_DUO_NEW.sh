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
OUT_DIR_RAW="$(yaml_get '.output_dir')"
SECS="$(yaml_get '.secs_per_run')"
NUM_RUNS="$(yaml_get '.num_runs')"

HOUSEKEEPING="$(yaml_get '.housekeeping')"
EXPERIMENT_CPUS="$(yaml_get '.experiment_cpus')"

MAHI_CORE="$(yaml_get '.cores.mahi_core')"
SERVER_CORE_L4S="$(yaml_get '.cores.server_core_l4s')"
CLIENT_CORE_L4S="$(yaml_get '.cores.client_core_l4s')"
SERVER_CORE_CLASSIC="$(yaml_get '.cores.server_core_classic')"
CLIENT_CORE_CLASSIC="$(yaml_get '.cores.client_core_classic')"

TRACE_UP="$(yaml_get '.traces.up')"
TRACE_DOWN="$(yaml_get '.traces.down')"

QUEUE_TYPE="$(yaml_get '.queue.type')"
Q_PACKETS="$(yaml_get '.queue.packets')"
Q_TARGET="$(yaml_get '.queue.target')"
Q_TUPDATE="$(yaml_get '.queue.tupdate')"
Q_ALPHA="$(yaml_get '.queue.alpha')"
Q_BETA="$(yaml_get '.queue.beta')"

# ---- flow configs (CONCURRENT) ----
CLASSIC_RATE="$(yaml_get '.flows.classic.rate')"
CLASSIC_PACKET_LEN="$(yaml_get '.flows.classic.packet_len')"
CLASSIC_TOS="$(yaml_get '.flows.classic.tos')"          # usually 0 (Not-ECT)

L4S_TOS="$(yaml_get '.flows.l4s.tos')"                  # usually 1 (ECT(1))
L4S_CC="$(yaml_get '.flows.l4s.cc')"                    # usually prague

BASE_PORT="$(yaml_get '.flows.base_port')"

# defaults
: "${SECS:=30}"
: "${NUM_RUNS:=1}"
: "${CLASSIC_RATE:=12M}"
: "${CLASSIC_PACKET_LEN:=1200}"
: "${CLASSIC_TOS:=0}"
: "${L4S_TOS:=1}"
: "${L4S_CC:=prague}"
: "${BASE_PORT:=5300}"

# --- normalize OUT_DIR to absolute + fix permissions for RUN_USER ---
OUT_DIR="$(realpath -m "$OUT_DIR_RAW")"
sudo -u "$RUN_USER" mkdir -p "$OUT_DIR"
chown -R "$RUN_USER:$RUN_USER" "$OUT_DIR" 2>/dev/null || true
chmod 755 "$OUT_DIR" 2>/dev/null || true

QUEUE_ARGS="packets=${Q_PACKETS},target=${Q_TARGET},tupdate=${Q_TUPDATE},alpha=${Q_ALPHA},beta=${Q_BETA}"

# --- pick next index automatically based on existing output files ---
next_index() {
  local max=-1
  shopt -s nullglob
  for f in "$OUT_DIR"/output_duo_*.txt; do
    local base="${f##*/}"
    local num="${base##*_}"
    num="${num%.txt}"
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    (( num > max )) && max="$num"
  done
  shopt -u nullglob
  echo $((max + 1))
}
START_INDEX="$(next_index)"

# --- HOST preflight: ensure prague exists in kernel ---
host_ensure_prague_available() {
  echo "[*] Host preflight: ensuring Prague CC is available in kernel..."
  local avail
  avail="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"
  if ! grep -qw prague <<<"$avail"; then
    echo "[*] Host: Prague not listed, trying: modprobe tcp_prague"
    sudo modprobe tcp_prague 2>/dev/null || true
    avail="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"
  fi
  if ! grep -qw prague <<<"$avail"; then
    echo "[!] ERROR: Prague congestion control not available on this kernel."
    echo "    tcp_available_congestion_control: $avail"
    exit 2
  fi
  echo "[*] Host: Prague is available."
}

# --- NS preflight: set/verify prague + ecn INSIDE the namespace (call from inside mm-link) ---
ns_force_prague_and_ecn() {
  # we are already inside the mm-link netns here
  sysctl -w net.ipv4.tcp_ecn=1 >/dev/null
  sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null

  local cc ecn avail
  cc="$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || true)"
  ecn="$(sysctl -n net.ipv4.tcp_ecn 2>/dev/null || true)"
  avail="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"

  echo "[ns] tcp_available_congestion_control=$avail"
  echo "[ns] tcp_congestion_control=$cc tcp_ecn=$ecn"

  [[ "$cc" == "prague" ]] || { echo "[!] NS ERROR: tcp_congestion_control != prague"; exit 21; }
  [[ "$ecn" != "0" ]] || { echo "[!] NS ERROR: tcp_ecn disabled"; exit 22; }
}

# --- experiment function (RUNS AS ROOT INSIDE SHIELD) ---
run_mahi_duo_root() {
  local SECS="$1"
  local IDX="$2"

  local PORT_CLASSIC=$((BASE_PORT + (2 * IDX)))
  local PORT_L4S=$((BASE_PORT + (2 * IDX) + 1))

  local OUT_FILE="${OUT_DIR}/output_duo_${IDX}.txt"
  local FLOW_LOG_CLASSIC="${OUT_DIR}/iperf_classic_${IDX}.txt"
  local FLOW_LOG_L4S="${OUT_DIR}/iperf_l4s_${IDX}.txt"

  echo "[*] Cleaning up Mahimahi + iperf (root)..."
  pkill -9 -x mm-link 2>/dev/null || true
  pkill -9 -x mm-delay 2>/dev/null || true
  pkill -9 -x iperf3 2>/dev/null || true

  for ns in $(ip netns list | awk '{print $1}' | grep '^mm-' || true); do
    ip netns del "$ns" 2>/dev/null || true
  done
  sleep 1

  # Host: ensure module/availability so namespace can use it
  host_ensure_prague_available

  echo "[*] Starting TWO iperf3 servers (root):"
  echo "    - classic UDP server: port $PORT_CLASSIC (core $SERVER_CORE_CLASSIC)"
  echo "    - l4s TCP server:    port $PORT_L4S (core $SERVER_CORE_L4S)"

  taskset -c "$SERVER_CORE_CLASSIC" iperf3 -s -p "$PORT_CLASSIC" >/dev/null 2>&1 &
  local PID_SRV_CLASSIC=$!

  taskset -c "$SERVER_CORE_L4S" iperf3 -s -p "$PORT_L4S" --one-off --interval 1 >/dev/null 2>&1 &
  local PID_SRV_L4S=$!

  sleep 1

  echo "[*] Running ONE Mahimahi instance (shared bottleneck) on core $MAHI_CORE as user=$RUN_USER (idx=$IDX)..."

  local USER_HOME
  USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  : "${USER_HOME:=/home/$RUN_USER}"

  sudo -u "$RUN_USER" touch "$OUT_FILE" "$FLOW_LOG_CLASSIC" "$FLOW_LOG_L4S"
  chmod 644 "$OUT_FILE" "$FLOW_LOG_CLASSIC" "$FLOW_LOG_L4S" 2>/dev/null || true

  sudo -u "$RUN_USER" env -i \
    HOME="$USER_HOME" USER="$RUN_USER" LOGNAME="$RUN_USER" \
    PATH="$PATH" \
    MM_NO_X=1 \
    bash -lc "
      set -euo pipefail

      taskset -c '$MAHI_CORE' mm-delay 0 mm-link \
        --uplink-queue='$QUEUE_TYPE' \
        --uplink-queue-args='$QUEUE_ARGS' \
        '$TRACE_UP' '$TRACE_DOWN' -- bash -lc '
          set -euo pipefail

          # Force Prague + ECN INSIDE this namespace (the thing you were missing)
          $(declare -f ns_force_prague_and_ecn)
          ns_force_prague_and_ecn

          echo \"[+] CONCURRENT FLOWS: classic=UDP(port=$PORT_CLASSIC rate=$CLASSIC_RATE len=$CLASSIC_PACKET_LEN tos=$CLASSIC_TOS) | l4s=TCP(port=$PORT_L4S cc=$L4S_CC tos=$L4S_TOS)\"

          (
            taskset -c $CLIENT_CORE_CLASSIC iperf3 -c 10.0.0.1 -p $PORT_CLASSIC -u \
              -b $CLASSIC_RATE -l $CLASSIC_PACKET_LEN -t $SECS --tos $CLASSIC_TOS --interval 1 \
              2>&1 | tee \"$FLOW_LOG_CLASSIC\"
          ) & pid_classic=\$!

          (
            taskset -c $CLIENT_CORE_L4S iperf3 -c 10.0.0.1 -p $PORT_L4S \
              -t $SECS --tos $L4S_TOS --interval 1 \
              -C $L4S_CC \
              2>&1 | tee \"$FLOW_LOG_L4S\"
          ) & pid_l4s=\$!

          wait \"\$pid_classic\" \"\$pid_l4s\"
          echo \"[+] Both flows done.\"
        ' | tee '$OUT_FILE'
    "

  kill "$PID_SRV_CLASSIC" 2>/dev/null || true
  kill "$PID_SRV_L4S" 2>/dev/null || true
  echo "[*] Done. Logs in $OUT_DIR"
}

# --- CPU shielding setup (requires root) ---
echo "[*] Using config: $CFG"
echo "[*] Output dir: $OUT_DIR"
echo "[*] Resetting cset..."
sudo cset shield --reset || true

echo "[*] Shielding CPUs: $EXPERIMENT_CPUS (housekeeping: $HOUSEKEEPING)"
sudo cset shield --cpu="$EXPERIMENT_CPUS" --kthread=on

for r in $(seq 0 $((NUM_RUNS - 1))); do
  idx=$((START_INDEX + r))
  echo ""
  echo "[*] ===== RUN index=$idx (duo: classic UDP + l4s TCP concurrently) ====="

  sudo cset shield --exec -- bash -lc "
    export RUN_USER='$RUN_USER'
    export MAHI_CORE='$MAHI_CORE'
    export SERVER_CORE_L4S='$SERVER_CORE_L4S'
    export CLIENT_CORE_L4S='$CLIENT_CORE_L4S'
    export SERVER_CORE_CLASSIC='$SERVER_CORE_CLASSIC'
    export CLIENT_CORE_CLASSIC='$CLIENT_CORE_CLASSIC'

    export QUEUE_TYPE='$QUEUE_TYPE'
    export QUEUE_ARGS='$QUEUE_ARGS'
    export TRACE_UP='$TRACE_UP'
    export TRACE_DOWN='$TRACE_DOWN'
    export OUT_DIR='$OUT_DIR'

    export CLASSIC_RATE='$CLASSIC_RATE'
    export CLASSIC_PACKET_LEN='$CLASSIC_PACKET_LEN'
    export CLASSIC_TOS='$CLASSIC_TOS'

    export L4S_TOS='$L4S_TOS'
    export L4S_CC='$L4S_CC'

    export BASE_PORT='$BASE_PORT'

    $(declare -f host_ensure_prague_available)
    $(declare -f run_mahi_duo_root)
    run_mahi_duo_root '$SECS' '$idx'
  "

  sleep 3
done

echo "[*] Releasing cset..."
sudo cset shield --reset
