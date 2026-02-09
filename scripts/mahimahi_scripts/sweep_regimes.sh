#!/usr/bin/env bash
set -euo pipefail

BASE_CFG="${1:-exp.yaml}"
SWEEP_CFG="${2:-sweep.yaml}"
RUN_SCRIPT="${3:-./single.sh}"

OUTROOT="runs"
mkdir -p "$OUTROOT"

# Load regimes as JSON lines
readarray -t REGIMES_JSONL < <(python3 - "$SWEEP_CFG" <<'PY'
import sys, yaml, json
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f) or {}
regs = (d.get("sweep") or {}).get("regimes") or []
if not isinstance(regs, list):
    raise SystemExit("sweep.regimes must be a list")
for r in regs:
    print(json.dumps(r))
PY
)

if [[ ${#REGIMES_JSONL[@]} -eq 0 ]]; then
  echo "[!] No regimes found in $SWEEP_CFG at sweep.regimes" >&2
  exit 2
fi

total_runs=${#REGIMES_JSONL[@]}
echo "[*] Total regimes = $total_runs"
echo "[*] Output root: $OUTROOT"

run_idx=0

for reg_json in "${REGIMES_JSONL[@]}"; do
  run_idx=$((run_idx + 1))

  reg_name="$(python3 - <<PY
import json
r=json.loads('''$reg_json''')
if "name" not in r:
    raise SystemExit("Each regime must have a name")
print(r["name"])
PY
)"

  # Safe folder name (but preserves your semantic naming)
  reg_slug="$(echo "$reg_name" | tr -cs 'A-Za-z0-9._+-' '_' )"
  rundir="${OUTROOT}/${reg_slug}"

  if [[ -e "$rundir" ]]; then
    echo "[!] Refusing to overwrite existing directory: $rundir" >&2
    exit 2
  fi

  mkdir -p "$rundir"
  cfg="${rundir}/exp.yaml"
  log="${rundir}/run.log"

  # Patch exp.yaml for this regime
  python3 - "$BASE_CFG" "$cfg" "$rundir" "$reg_json" <<'PY'
import sys, yaml, json, os

src, dst, rundir = sys.argv[1], sys.argv[2], sys.argv[3]
reg = json.loads(sys.argv[4])

def die(msg):
    raise SystemExit(msg)

# Required fields
for k in ("delay_ms", "traces", "flows", "name"):
    if k not in reg:
        die(f"regime missing '{k}'")

if not isinstance(reg["traces"], dict):
    die("traces must be a dict")
if "up" not in reg["traces"] or "down" not in reg["traces"]:
    die("traces must contain up and down")

with open(src) as f:
    d = yaml.safe_load(f) or {}

# Delay
d.setdefault("mahimahi", {})["delay_ms"] = int(reg["delay_ms"])

# Traces
d["traces"] = {
    "up": reg["traces"]["up"],
    "down": reg["traces"]["down"],
}

# Flows (exact override)
flows = dict(reg["flows"])
base_port = flows.get("base_port", (d.get("flows") or {}).get("base_port", 5300))
flows["base_port"] = base_port
d["flows"] = flows

# Optional queue override
if isinstance(reg.get("queue"), dict):
    d.setdefault("queue", {})
    if "packets" in reg["queue"]:
        d["queue"]["packets"] = int(reg["queue"]["packets"])

# Output directory
d["output_dir"] = os.path.join(os.path.abspath(rundir), "outputs")

# Metadata
d["regime_name"] = reg["name"]

with open(dst, "w") as f:
    yaml.safe_dump(d, f, sort_keys=False)
PY

  # Print regime summary
  readarray -t meta < <(python3 - <<PY
import json
r=json.loads('''$reg_json''')
print(r["delay_ms"])
qp = ""
if isinstance(r.get("queue"), dict) and "packets" in r["queue"]:
    qp = str(r["queue"]["packets"])
print(qp)
PY
)

  delay="${meta[0]}"
  qpkts="${meta[1]:-}"

  echo
  echo "=================================================="
  echo "[*] Regime:       $reg_name"
  echo "[*] Delay (ms):   $delay"
  if [[ -n "$qpkts" ]]; then
    echo "[*] Queue limit:  $qpkts packets"
  else
    echo "[*] Queue limit:  <base config>"
  fi
  echo "[*] Dir:          $rundir"
  echo "=================================================="
  echo

  ( set -x; sudo bash "$RUN_SCRIPT" "$cfg" ) 2>&1 | tee "$log"
done

echo
echo "[✓] All regimes completed successfully."
