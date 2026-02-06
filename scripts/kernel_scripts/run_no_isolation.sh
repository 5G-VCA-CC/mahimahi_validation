#!/usr/bin/env bash
set -Eeuo pipefail
trap 'rc=$?; echo "ERR rc=$rc at line $LINENO: $BASH_COMMAND" >&2; exit $rc' ERR

# ============================================================
# Config loader (PyYAML)
# ============================================================
CFG=${1:-config_no_i.yaml}
if [[ ! -f "$CFG" ]]; then
  echo "Error: config file not found: $CFG"
  exit 1
fi

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

# ============================================================
# Read config
# ============================================================
SECS_LOG="$(yget runs.secs_per_run)"; SECS_LOG="${SECS_LOG:-30}"
NUM_RUNS="$(yget runs.num_runs)"; NUM_RUNS="${NUM_RUNS:-1}"

SETUP_SEC="$(yget runs.setup_sec)"; SETUP_SEC="${SETUP_SEC:-3}"
WARMUP_SEC="$(yget runs.warmup_sec)"; WARMUP_SEC="${WARMUP_SEC:-0}"
COOLDOWN_SEC="$(yget runs.cooldown_sec)"; COOLDOWN_SEC="${COOLDOWN_SEC:-3}"

NS_S="$(yget net.ns_s)"; NS_S="${NS_S:-ns_s}"
NS_R="$(yget net.ns_r)"; NS_R="${NS_R:-ns_r}"
VETH_DEV="$(yget net.veth_dev)"; VETH_DEV="${VETH_DEV:-veth-s}"
DST_IP="$(yget net.dst_ip)"; DST_IP="${DST_IP:-172.20.1.2}"

PORT_CLASSIC="$(yget net.port_classic)"; PORT_CLASSIC="${PORT_CLASSIC:-5202}"
PORT_L4S="$(yget net.port_l4s)"; PORT_L4S="${PORT_L4S:-5203}"

RTT_MS="$(yget net.rtt_ms)"; RTT_MS="${RTT_MS:-0}"

# IMPORTANT: point this at your custom tc if system tc doesn't recognize dualpi2
TC_BIN="$(yget paths.tc_bin)"; TC_BIN="${TC_BIN:-tc}"

RATE="$(yget qdisc.rate)"; RATE="${RATE:-12mbit}"
BURST="$(yget qdisc.burst)"; BURST="${BURST:-1k}"
TARGET="$(yget qdisc.target)"; TARGET="${TARGET:-1ms}"
TUPDATE="$(yget qdisc.tupdate)"; TUPDATE="${TUPDATE:-16ms}"
LIMIT="$(yget qdisc.limit)"; LIMIT="${LIMIT:-15040000}"
ALPHA="$(yget qdisc.alpha)"; ALPHA="${ALPHA:-0.16}"
BETA="$(yget qdisc.beta)"; BETA="${BETA:-3.2}"

# --- Flow config (NEW) ---
CLASSIC_MODE="$(yget flows.classic.mode)"; CLASSIC_MODE="${CLASSIC_MODE:-udp}"   # udp|tcp
CLASSIC_CC="$(yget flows.classic.cc)"; CLASSIC_CC="${CLASSIC_CC:-cubic}"        # if classic.mode=tcp

L4S_CC="$(yget flows.l4s.cc)"; L4S_CC="${L4S_CC:-prague}"                       # TCP CC for l4s

CLASSIC_BW="$(yget flows.classic.bandwidth)"; CLASSIC_BW="${CLASSIC_BW:-12M}"   # if classic.mode=udp
CLASSIC_DGRAM_LEN="$(yget flows.classic.datagram_len)"; CLASSIC_DGRAM_LEN="${CLASSIC_DGRAM_LEN:-1200}"  # if classic.mode=udp

LOG_DIR="$(yget logging.dir)"; LOG_DIR="${LOG_DIR:-./tmp}"
QDISC_SAMPLE_SEC="$(yget logging.qdisc_sample_sec)"; QDISC_SAMPLE_SEC="${QDISC_SAMPLE_SEC:-0.016}"

SECS_IPERF=$(( WARMUP_SEC + SECS_LOG ))
LOG_TIME=$(( SECS_LOG ))

TOS_CLASSIC="0x00"  # Not-ECT
TOS_L4S="0x01"      # ECT(1)

LOG_DIR="$(realpath -m "$LOG_DIR")"
sudo mkdir -p "$LOG_DIR"
sudo chmod 777 "$LOG_DIR"

# ============================================================
# Helpers
# ============================================================
next_free_idx() {
  local i=0
  while [[ -e "$LOG_DIR/qdisc_${i}.log" ]]; do
    ((++i))   # pre-increment avoids set -e trap on i==0
  done
  echo "$i"
}

cleanup_run() {
  sudo ip netns exec "$NS_S" pkill -9 iperf3 2>/dev/null || true
  sudo ip netns exec "$NS_R" pkill -9 iperf3 2>/dev/null || true
}

# ============================================================
# CLEARLY LABELED: Create namespaces + attach veth pair
# ============================================================
reset_namespaces_fresh() {
  cleanup_run

  sudo ip netns del "$NS_S" 2>/dev/null || true
  sudo ip netns del "$NS_R" 2>/dev/null || true

  sudo ip link del veth-s 2>/dev/null || true
  sudo ip link del veth-r 2>/dev/null || true

  sudo ip netns add "$NS_S"
  sudo ip netns add "$NS_R"

  sudo ip link add veth-s type veth peer name veth-r

  sudo ip link set veth-s netns "$NS_S"
  sudo ip link set veth-r netns "$NS_R"

  sudo ip netns exec "$NS_S" ip addr add 172.20.1.1/30 dev veth-s
  sudo ip netns exec "$NS_R" ip addr add 172.20.1.2/30 dev veth-r

  sudo ip netns exec "$NS_S" ip link set lo up
  sudo ip netns exec "$NS_R" ip link set lo up
  sudo ip netns exec "$NS_S" ip link set veth-s up
  sudo ip netns exec "$NS_R" ip link set veth-r up

  sudo ip netns exec "$NS_S" ethtool -K veth-s tso off gso off gro off lro off 2>/dev/null || true
  sudo ip netns exec "$NS_R" ethtool -K veth-r tso off gso off gro off lro off 2>/dev/null || true
}

# ============================================================
# Enable Prague + ECN (best effort)
# ============================================================
enable_l4s_prague_in_both_namespaces() {
  sudo modprobe tcp_prague 2>/dev/null || true

  for ns in "$NS_S" "$NS_R"; do
    sudo ip netns exec "$ns" sysctl -w net.ipv4.tcp_ecn=1 >/dev/null 2>&1 || true
    if sudo ip netns exec "$ns" test -e /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null; then
      # NOTE: setting default CC to prague is fine; we override per-flow via iperf3 -C anyway
      sudo ip netns exec "$ns" sysctl -w net.ipv4.tcp_congestion_control=prague >/dev/null 2>&1 || true
    fi
  done
}

# ============================================================
# Apply RTT delay via IFB + netem
# ============================================================
apply_rtt_netem_delay() {
  local rtt_ms="${RTT_MS:-0}"
  if (( rtt_ms <= 0 )); then
    return 0
  fi

  local fwd_ms=$(( rtt_ms / 2 ))
  local rev_ms=$(( rtt_ms - fwd_ms ))

  sudo modprobe ifb 2>/dev/null || true

  _add_ingress_delay() {
    local ns="$1"
    local in_dev="$2"
    local ifb_dev="$3"
    local delay_ms="$4"

    sudo ip netns exec "$ns" ip link add "$ifb_dev" type ifb 2>/dev/null || true
    sudo ip netns exec "$ns" ip link set "$ifb_dev" up

    sudo ip netns exec "$ns" "$TC_BIN" qdisc del dev "$in_dev" ingress 2>/dev/null || true
    sudo ip netns exec "$ns" "$TC_BIN" qdisc del dev "$ifb_dev" root 2>/dev/null || true

    sudo ip netns exec "$ns" "$TC_BIN" qdisc add dev "$in_dev" handle ffff: ingress

    sudo ip netns exec "$ns" "$TC_BIN" filter add dev "$in_dev" parent ffff: protocol ip u32 \
      match u32 0 0 action mirred egress redirect dev "$ifb_dev"

    sudo ip netns exec "$ns" "$TC_BIN" qdisc add dev "$ifb_dev" root netem delay "${delay_ms}ms"
  }

  _add_ingress_delay "$NS_R" "veth-r" "ifb-fwd" "$fwd_ms"
  _add_ingress_delay "$NS_S" "veth-s" "ifb-rev" "$rev_ms"
}

# ============================================================
# Apply DualPI2 qdisc
# ============================================================
apply_dualpi2_qdisc() {
  sudo ip netns exec "$NS_S" "$TC_BIN" qdisc del dev "$VETH_DEV" root 2>/dev/null || true
  sudo ip netns exec "$NS_S" "$TC_BIN" qdisc add dev "$VETH_DEV" root handle 1: htb default 10
  sudo ip netns exec "$NS_S" "$TC_BIN" class add dev "$VETH_DEV" parent 1: classid 1:10 htb \
    rate "$RATE" ceil "$RATE" burst "$BURST"

  sudo ip netns exec "$NS_S" "$TC_BIN" qdisc add dev "$VETH_DEV" parent 1:10 handle 2: dualpi2 \
    target "$TARGET" tupdate "$TUPDATE" limit "$LIMIT" alpha "$ALPHA" beta "$BETA"
}

# ============================================================
# Logger (no pinning / no RT scheduling)
# ============================================================
start_qdisc_logger() {
  local idx="$1"
  local qdisc_log="$LOG_DIR/qdisc_${idx}.log"
  : > "$qdisc_log"

  local dt_ns start end
  dt_ns="$(awk -v dt="$QDISC_SAMPLE_SEC" 'BEGIN{printf "%.0f\n", dt*1e9}')"

  start="$("./nsclock")"
  end=$(( start + SECS_LOG*1000000000 ))

  {
    echo "RUN_START_TS_NS $start"
    echo "DT_NS $dt_ns"
  } >> "$qdisc_log"

  (
    timeout "${LOG_TIME}s" \
      ip netns exec "$NS_S" env \
        VETH_DEV="$VETH_DEV" \
        QDISC_LOG="$qdisc_log" \
        END_NS="$end" \
        DT_NS="$dt_ns" \
        TC_BIN="$TC_BIN" \
        NSCLOCK="$(pwd)/nsclock" \
      bash -Eeuo pipefail -c '
        : "${VETH_DEV:?}" "${QDISC_LOG:?}" "${END_NS:?}" "${DT_NS:?}" "${TC_BIN:?}" "${NSCLOCK:?}"

        i=1
        START_NS=$("$NSCLOCK")
        next=$((START_NS + DT_NS)) 

        while :; do
          now=$("$NSCLOCK")
          if (( now >= END_NS )); then break; fi

          t0=$("$NSCLOCK")
          out=$("$TC_BIN" -s qdisc show dev "$VETH_DEV" parent 1:10)
          t1=$("$NSCLOCK")

          tick_ns=$("$NSCLOCK")

          {
            echo "TICK_NS $tick_ns"
            echo "TC_DUR_NS $((t1 - t0))"
            echo "NEXT $next"
            echo "PLACEMENT i=$i"
            echo "$out"
            echo
          } >> "$QDISC_LOG"
          
          now=$("$NSCLOCK")
	  diff_ns=$(( next - now ))
          if (( diff_ns > 0 )); then
             sec=$((diff_ns / 1000000000))
             nsec=$((diff_ns % 1000000000))
             sleep "$(printf "%d.%09d" "$sec" "$nsec")"
          fi

          i=$((i+1))
          next=$((START_NS + i*DT_NS))

        done

        echo "RUN_END_TS_NS $("$NSCLOCK")" >> "$QDISC_LOG"
      '
  ) &

  echo $!
}

# ============================================================
# Run both iperf flows concurrently (NO PINNING)
# ============================================================
run_both_flows() {
  local idx="$1"
  local marker_classic="$LOG_DIR/run_${idx}.classic_started"
  local marker_l4s="$LOG_DIR/run_${idx}.l4s_started"
  rm -f "$marker_classic" "$marker_l4s" 2>/dev/null || true

  sudo ip netns exec "$NS_R" iperf3 -s -p "$PORT_CLASSIC" >/dev/null 2>&1 &
  local srv_classic=$!
  sudo ip netns exec "$NS_R" iperf3 -s -p "$PORT_L4S" >/dev/null 2>&1 &
  local srv_l4s=$!

  sleep 1
  sleep "$SETUP_SEC"

  # ---- Classic flow (UDP or TCP) ----
  if [[ "$CLASSIC_MODE" == "udp" ]]; then
    sudo ip netns exec "$NS_S" iperf3 -c "$DST_IP" -p "$PORT_CLASSIC" -u \
      -b "$CLASSIC_BW" -l "$CLASSIC_DGRAM_LEN" -t "$SECS_IPERF" --tos "$TOS_CLASSIC" >/dev/null 2>&1 &
  else
    sudo ip netns exec "$NS_S" iperf3 -c "$DST_IP" -p "$PORT_CLASSIC" \
      -t "$SECS_IPERF" -C "$CLASSIC_CC" --tos "$TOS_CLASSIC" >/dev/null 2>&1 &
  fi
  local cli_classic=$!

  # ---- L4S flow (TCP with configurable CC) ----
  sudo ip netns exec "$NS_S" iperf3 -c "$DST_IP" -p "$PORT_L4S" \
    -t "$SECS_IPERF" -C "$L4S_CC" --tos "$TOS_L4S" >/dev/null 2>&1 &
  local cli_l4s=$!

  wait "$cli_classic" 2>/dev/null || true
  wait "$cli_l4s" 2>/dev/null || true

  sudo kill "$srv_classic" "$srv_l4s" >/dev/null 2>&1 || true
  cleanup_run
}

# ============================================================
# One run
# ============================================================
run_once() {
  local idx="$1"
  local qdisc_log="$LOG_DIR/qdisc_${idx}.log"
  local marker_classic="$LOG_DIR/run_${idx}.classic_started"
  local marker_l4s="$LOG_DIR/run_${idx}.l4s_started"

  reset_namespaces_fresh
  enable_l4s_prague_in_both_namespaces
  apply_rtt_netem_delay
  apply_dualpi2_qdisc

  run_both_flows "$idx" &
  local EXP_PID=$!

  if (( WARMUP_SEC > 0 )); then
    sleep "$WARMUP_SEC"
  fi

  sleep 1
  sleep "$SETUP_SEC"

  local QDISC_PID
  QDISC_PID="$(start_qdisc_logger "$idx")"
  wait "$QDISC_PID" 2>/dev/null || true

  cleanup_run
  wait "$EXP_PID" 2>/dev/null || true

  echo "Saved: $qdisc_log"
}

# ============================================================
# Main
# ============================================================
echo "Using config: $CFG"
echo "LOG_DIR=$LOG_DIR"
echo "CLASSIC_MODE=$CLASSIC_MODE (udp|tcp)"
echo "CLASSIC_CC=$CLASSIC_CC (if classic is tcp)"
echo "L4S_CC=$L4S_CC"

for ((run=0; run<NUM_RUNS; run++)); do
  IDX="$(next_free_idx)"
  run_once "$IDX"
  sleep "$COOLDOWN_SEC"
done
s
