#!/usr/bin/env bash
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
  if not isinstance(cur, dict) or part not in cur:
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

# --- monotonic-ish timestamp in ns (NO python forks) ---
mono_ns_uptime() {
  awk '{printf "%.0f\n", $1*1000000000}' /proc/uptime
}

# ----------------------------
# Read config
# ----------------------------
# tc path can come from:
#   1) YAML: tools.tc_path
#   2) env:  TC_BIN
#   3) default hardcoded path
TC_BIN_CFG="$(yget tools.tc_path)"
TC_BIN="${TC_BIN_CFG:-${TC_BIN:-/home/linghe-zhang/iproute2/tc/tc}}"

SECS_LOG="$(yget runs.secs_per_run)"; SECS_LOG="${SECS_LOG:-30}"
NUM_RUNS="$(yget runs.num_runs)"; NUM_RUNS="${NUM_RUNS:-1}"

SETUP_SEC="$(yget runs.setup_sec)"; SETUP_SEC="${SETUP_SEC:-3}"
WARMUP_SEC="$(yget runs.warmup_sec)"; WARMUP_SEC="${WARMUP_SEC:-2}"
COOLDOWN_SEC="$(yget runs.cooldown_sec)"; COOLDOWN_SEC="${COOLDOWN_SEC:-3}"

NS_S="$(yget net.ns_s)"; NS_S="${NS_S:-ns_s}"
NS_R="$(yget net.ns_r)"; NS_R="${NS_R:-ns_r}"
VETH_DEV="$(yget net.veth_dev)"; VETH_DEV="${VETH_DEV:-veth-s}"
DST_IP="$(yget net.dst_ip)"; DST_IP="${DST_IP:-172.20.1.2}"

PORT_CLASSIC="$(yget net.port_classic)"; PORT_CLASSIC="${PORT_CLASSIC:-5202}"
PORT_L4S="$(yget net.port_l4s)"; PORT_L4S="${PORT_L4S:-5203}"

RATE="$(yget qdisc.rate)"; RATE="${RATE:-12mbit}"
BURST="$(yget qdisc.burst)"; BURST="${BURST:-1k}"
TARGET="$(yget qdisc.target)"; TARGET="${TARGET:-1ms}"
TUPDATE="$(yget qdisc.tupdate)"; TUPDATE="${TUPDATE:-16ms}"
LIMIT="$(yget qdisc.limit)"; LIMIT="${LIMIT:-15040000}"
ALPHA="$(yget qdisc.alpha)"; ALPHA="${ALPHA:-0.16}"
BETA="$(yget qdisc.beta)"; BETA="${BETA:-3.2}"

CLASSIC_BW="$(yget flows.classic.bandwidth)"; CLASSIC_BW="${CLASSIC_BW:-12M}"
CLASSIC_DGRAM_LEN="$(yget flows.classic.datagram_len)"; CLASSIC_DGRAM_LEN="${CLASSIC_DGRAM_LEN:-1200}"

# TCP is congestion-controlled; no app pacing
L4S_PARALLEL="$(yget flows.l4s.parallel)"; L4S_PARALLEL="${L4S_PARALLEL:-1}"

LOG_DIR="$(yget logging.dir)"; LOG_DIR="${LOG_DIR:-./tmp}"

SAMPLE_SEC="$(yget logging.sample_sec)"; SAMPLE_SEC="${SAMPLE_SEC:-0.016}"
QDISC_SAMPLE_SEC="$(yget logging.qdisc_sample_sec)"; QDISC_SAMPLE_SEC="${QDISC_SAMPLE_SEC:-$SAMPLE_SEC}"

TIMEOUT_EXTRA="$(yget logging.timeout_extra)"; TIMEOUT_EXTRA="${TIMEOUT_EXTRA:-12}"

EXPERIMENT_CPUS="$(yget cpu.experiment)"; EXPERIMENT_CPUS="${EXPERIMENT_CPUS:-4-7}"

SENDER_CORE_L4S="$(yget cpu.sender_core_l4s)"; SENDER_CORE_L4S="${SENDER_CORE_L4S:-4}"
RECEIVER_CORE_L4S="$(yget cpu.receiver_core_l4s)"; RECEIVER_CORE_L4S="${RECEIVER_CORE_L4S:-5}"

UDP_CORE_OFFSET="$(yget cpu.udp_core_offset)"; UDP_CORE_OFFSET="${UDP_CORE_OFFSET:-2}"
SENDER_CORE_CLASSIC="$(yget cpu.sender_core_classic)"
RECEIVER_CORE_CLASSIC="$(yget cpu.receiver_core_classic)"
if [[ -z "${SENDER_CORE_CLASSIC:-}" ]]; then
  SENDER_CORE_CLASSIC=$(( SENDER_CORE_L4S + UDP_CORE_OFFSET ))
fi
if [[ -z "${RECEIVER_CORE_CLASSIC:-}" ]]; then
  RECEIVER_CORE_CLASSIC=$(( RECEIVER_CORE_L4S + UDP_CORE_OFFSET ))
fi

LOGGER_CORE="$(yget cpu.logger_core)"; LOGGER_CORE="${LOGGER_CORE:-3}"

if (( SECS_LOG <= 0 )); then
  echo "Error: runs.secs_per_run must be > 0 (got $SECS_LOG)"
  exit 1
fi

SECS_IPERF=$(( WARMUP_SEC + SECS_LOG ))
LOG_TIME=$(( SECS_LOG + TIMEOUT_EXTRA ))

TOS_CLASSIC="0x00"  # Not-ECT
TOS_L4S="0x01"      # ECT(1) (only meaningful if ECN negotiated)

# Make LOG_DIR absolute + writable
LOG_DIR="$(realpath -m "$LOG_DIR")"
sudo mkdir -p "$LOG_DIR"
sudo chmod 777 "$LOG_DIR"

# ----------------------------
# ORIGINAL SETUP (netns + veth + IPs + offloads)
# ----------------------------
original_setup() {
  sudo ip netns add "$NS_S" 2>/dev/null || true
  sudo ip netns add "$NS_R" 2>/dev/null || true

  # recreate veth cleanly
  sudo ip -n "$NS_S" link del veth-s 2>/dev/null || true
  sudo ip -n "$NS_R" link del veth-r 2>/dev/null || true
  sudo ip link del veth-s 2>/dev/null || true
  sudo ip link del veth-r 2>/dev/null || true

  sudo ip link add veth-s type veth peer name veth-r
  sudo ip link set veth-s netns "$NS_S"
  sudo ip link set veth-r netns "$NS_R"

  sudo ip netns exec "$NS_S" ip addr add 172.20.1.1/30 dev veth-s
  sudo ip netns exec "$NS_R" ip addr add 172.20.1.2/30 dev veth-r

  sudo ip netns exec "$NS_S" ip link set lo up
  sudo ip netns exec "$NS_R" ip link set lo up
  sudo ip netns exec "$NS_S" ip link set veth-s up
  sudo ip netns exec "$NS_R" ip link set veth-r up

  # disable offloads on sender egress
  sudo ip netns exec "$NS_S" ethtool -K veth-s tso off gso off gro off lro off 2>/dev/null || true
  sudo ip netns exec "$NS_R" ethtool -K veth-r tso off gso off gro off lro off 2>/dev/null || true
}

# ----------------------------
# KEY FIX: enable ECN + prague in BOTH namespaces
# ----------------------------
enable_l4s_prague_in_both_namespaces() {
  sudo modprobe tcp_prague 2>/dev/null || true

  sudo ip netns exec "$NS_S" sysctl -w net.ipv4.tcp_ecn=1 >/dev/null
  sudo ip netns exec "$NS_R" sysctl -w net.ipv4.tcp_ecn=1 >/dev/null

  sudo ip netns exec "$NS_S" sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null
  sudo ip netns exec "$NS_R" sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null

  echo "[*] ns_s tcp_ecn=$(sudo ip netns exec "$NS_S" sysctl -n net.ipv4.tcp_ecn) tcp_cc=$(sudo ip netns exec "$NS_S" sysctl -n net.ipv4.tcp_congestion_control)"
  echo "[*] ns_r tcp_ecn=$(sudo ip netns exec "$NS_R" sysctl -n net.ipv4.tcp_ecn) tcp_cc=$(sudo ip netns exec "$NS_R" sysctl -n net.ipv4.tcp_congestion_control)"
  echo "[*] avail_cc(host)=$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true)"
}

next_free_idx() {
  local i=0
  while [[ -e "$LOG_DIR/qdisc_${i}.log" ]]; do
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

start_qdisc_logger_housekeeping() {
  local idx="$1"
  local qdisc_log="$LOG_DIR/qdisc_${idx}.log"

  echo "📊 Logging qdisc to $qdisc_log (core $LOGGER_CORE) for ${SECS_LOG}s (dt=${QDISC_SAMPLE_SEC})..."

  taskset -c "$LOGGER_CORE" timeout "${LOG_TIME}s" bash -eu -o pipefail -c '
    mono_ns_uptime() { awk "{printf \"%.0f\n\", \$1*1000000000}" /proc/uptime; }

    start=$(mono_ns_uptime)
    end=$(( start + SECS_LOG*1000000000 ))

    while :; do
      now=$(mono_ns_uptime)
      (( now >= end )) && break

      sleep "$QDISC_SAMPLE_SEC"

      {
        echo "TS_NS $now"
        sudo ip netns exec "\$NS_S" "\$TC_BIN" -s qdisc show dev "\$VETH_DEV"
      } >> "$QDISC_LOG"
    done
  ' \
    NS_S="$NS_S" \
    VETH_DEV="$VETH_DEV" \
    TC_BIN="$TC_BIN" \
    QDISC_LOG="$qdisc_log" \
    SECS_LOG="$SECS_LOG" \
    QDISC_SAMPLE_SEC="$QDISC_SAMPLE_SEC" &

  echo $!
}

run_both_flows_concurrently_in_shield() {
  local idx="$1"

  sudo cset shield --exec -- bash -lc "
    set -euo pipefail

    NS_S='$NS_S'
    NS_R='$NS_R'
    VETH_DEV='$VETH_DEV'
    DST_IP='$DST_IP'

    PORT_CLASSIC='$PORT_CLASSIC'
    PORT_L4S='$PORT_L4S'

    RATE='$RATE'
    BURST='$BURST'
    TARGET='$TARGET'
    TUPDATE='$TUPDATE'
    LIMIT='$LIMIT'
    ALPHA='$ALPHA'
    BETA='$BETA'

    SECS_IPERF='$SECS_IPERF'
    SETUP_SEC='$SETUP_SEC'

    CLASSIC_BW='$CLASSIC_BW'
    CLASSIC_DGRAM_LEN='$CLASSIC_DGRAM_LEN'

    L4S_PARALLEL='$L4S_PARALLEL'

    TOS_CLASSIC='$TOS_CLASSIC'
    TOS_L4S='$TOS_L4S'

    SENDER_CORE_L4S='$SENDER_CORE_L4S'
    RECEIVER_CORE_L4S='$RECEIVER_CORE_L4S'
    SENDER_CORE_CLASSIC='$SENDER_CORE_CLASSIC'
    RECEIVER_CORE_CLASSIC='$RECEIVER_CORE_CLASSIC'

    LOG_DIR='$LOG_DIR'
    IDX='$idx'

    run_pinned_ns() {
      local ns=\"\$1\"; shift
      local core=\"\$1\"; shift
      ip netns exec \"\$ns\" taskset -c \"\$core\" \"\$@\"
    }

    # --- ensure namespaces/veth exist + enable prague/ECN in both ---
    $(declare -f original_setup)
    $(declare -f enable_l4s_prague_in_both_namespaces)
    original_setup
    enable_l4s_prague_in_both_namespaces

    mkdir -p \"\$LOG_DIR\"
    rm -f \"\$LOG_DIR/run_\${IDX}.classic_started\" \"\$LOG_DIR/run_\${IDX}.l4s_started\" 2>/dev/null || true

    ip netns exec \"\$NS_S\" pkill -9 iperf3 2>/dev/null || true
    ip netns exec \"\$NS_R\" pkill -9 iperf3 2>/dev/null || true

    echo \"🔁 Resetting qdisc on \$VETH_DEV...\"
    ip netns exec \"\$NS_S\" tc qdisc del dev \"\$VETH_DEV\" root 2>/dev/null || true
    ip netns exec \"\$NS_S\" tc qdisc add dev \"\$VETH_DEV\" root handle 1: htb default 10
    ip netns exec \"\$NS_S\" tc class add dev \"\$VETH_DEV\" parent 1: classid 1:10 htb \
      rate \"\$RATE\" ceil \"\$RATE\" burst \"\$BURST\"

    ip netns exec \"\$NS_S\" tc qdisc add dev \"\$VETH_DEV\" parent 1:10 handle 2: dualpi2 \
      target \"\$TARGET\" tupdate \"\$TUPDATE\" limit \"\$LIMIT\" alpha \"\$ALPHA\" beta \"\$BETA\"

    echo \"📡 Starting TWO iperf3 servers in \$NS_R...\"
    run_pinned_ns \"\$NS_R\" \"\$RECEIVER_CORE_CLASSIC\" iperf3 -s -p \"\$PORT_CLASSIC\" >/dev/null 2>&1 &
    srv_classic=\$!
    run_pinned_ns \"\$NS_R\" \"\$RECEIVER_CORE_L4S\" iperf3 -s -p \"\$PORT_L4S\" >/dev/null 2>&1 &
    srv_l4s=\$!

    sleep 1
    echo \"🧰 Setup delay \${SETUP_SEC}s...\"
    sleep \"\$SETUP_SEC\"

    echo \"🚀 Starting BOTH clients for \${SECS_IPERF}s...\"

    run_pinned_ns \"\$NS_S\" \"\$SENDER_CORE_CLASSIC\" iperf3 -c \"\$DST_IP\" -p \"\$PORT_CLASSIC\" -u \
      -b \"\$CLASSIC_BW\" -l \"\$CLASSIC_DGRAM_LEN\" -t \"\$SECS_IPERF\" --tos \"\$TOS_CLASSIC\" >/dev/null 2>&1 &
    cli_classic=\$!
    touch \"\$LOG_DIR/run_\${IDX}.classic_started\"

    # L4S TCP client: explicitly prague, ECN is enabled via sysctl
    run_pinned_ns \"\$NS_S\" \"\$SENDER_CORE_L4S\" iperf3 -c \"\$DST_IP\" -p \"\$PORT_L4S\" \
      -t \"\$SECS_IPERF\" -P \"\$L4S_PARALLEL\" -C prague --tos \"\$TOS_L4S\" >/dev/null 2>&1 &
    cli_l4s=\$!
    touch \"\$LOG_DIR/run_\${IDX}.l4s_started\"

    wait \"\$cli_classic\" 2>/dev/null || true
    wait \"\$cli_l4s\" 2>/dev/null || true

    kill \"\$srv_classic\" \"\$srv_l4s\" >/dev/null 2>&1 || true
    ip netns exec \"\$NS_S\" pkill -9 iperf3 2>/dev/null || true
    ip netns exec \"\$NS_R\" pkill -9 iperf3 2>/dev/null || true
  "
}

run_once() {
  local idx="$1"
  local qdisc_log="$LOG_DIR/qdisc_${idx}.log"
  local marker_classic="$LOG_DIR/run_${idx}.classic_started"
  local marker_l4s="$LOG_DIR/run_${idx}.l4s_started"

  echo
  echo "[*] === Run #$idx (CONCURRENT classic UDP + l4s TCP) ==="
  echo "[*] setup_sec=${SETUP_SEC}s, warmup_sec=${WARMUP_SEC}s, LOGGED=${SECS_LOG}s, iperf_total=${SECS_IPERF}s"
  echo "[*] qdisc rate=$RATE target=$TARGET tupdate=$TUPDATE"
  echo "[*] classic UDP: bw=$CLASSIC_BW port=$PORT_CLASSIC tos=$TOS_CLASSIC sender_core=$SENDER_CORE_CLASSIC receiver_core=$RECEIVER_CORE_CLASSIC"
  echo "[*] l4s TCP: P=$L4S_PARALLEL port=$PORT_L4S tos=$TOS_L4S sender_core=$SENDER_CORE_L4S receiver_core=$RECEIVER_CORE_L4S (prague+ECN in BOTH netns)"
  echo "[*] Log: $qdisc_log"
  echo "[*] Logger dt: qdisc=${QDISC_SAMPLE_SEC}s"

  rm -f "$marker_classic" "$marker_l4s" 2>/dev/null || true

  run_both_flows_concurrently_in_shield "$idx" &
  local EXP_PID=$!

  if (( WARMUP_SEC > 0 )); then
    echo "⏳ Warmup ${WARMUP_SEC}s (flows running, not logging)..."
    sleep "$WARMUP_SEC"
  fi

  echo "▶️  Logging for exactly ${SECS_LOG}s..."
  local QDISC_PID
  QDISC_PID="$(start_qdisc_logger_housekeeping "$idx")"

  wait "$EXP_PID" 2>/dev/null || true

  echo "🛑 Stopping logger and cleaning up..."
  cleanup_run "$QDISC_PID"

  echo "✅ Saved: $qdisc_log"
}

echo "[*] Using config: $CFG"
echo "[*] LOG_DIR=$LOG_DIR"
echo "[*] TC_BIN=$TC_BIN"

echo "[*] Resetting cset..."
sudo cset shield --reset >/dev/null 2>&1 || true

echo "[*] Shielding CPUs: experiment=$EXPERIMENT_CPUS"
sudo cset shield --cpu="$EXPERIMENT_CPUS" --kthread=on >/dev/null

for ((run=0; run<NUM_RUNS; run++)); do
  IDX="$(next_free_idx)"
  echo ""
  echo "[*] ===== RUN index=$IDX ====="
  run_once "$IDX"
  echo "[*] Cooling down ${COOLDOWN_SEC}s..."
  sleep "$COOLDOWN_SEC"
done

echo "[*] Releasing cset..."
sudo cset shield --reset >/dev/null 2>&1 || true

echo "[*] All runs complete."