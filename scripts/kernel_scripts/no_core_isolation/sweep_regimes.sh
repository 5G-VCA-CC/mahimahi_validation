#!/usr/bin/env bash
set -euo pipefail

BASE_CFG="${1:-config_no_isolation.yaml}"
SWEEP_CFG="${2:-sweep.yaml}"
RUN_SCRIPT="${3:-./run_no_isolation.sh}"   # <-- your existing script (unchanged), must accept CFG as $1

OUTROOT="runs"
mkdir -p "$OUTROOT"

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

total=${#REGIMES_JSONL[@]}
echo "[*] Total regimes = $total"
echo "[*] Output root   = $OUTROOT"

i=0
for reg_json in "${REGIMES_JSONL[@]}"; do
  i=$((i+1))

  reg_name="$(python3 - <<PY
import json
r=json.loads('''$reg_json''')
if "name" not in r:
    raise SystemExit("Each regime must have a name")
print(r["name"])
PY
)"

  # Safe folder name (no tr range bugs)
  reg_slug="$(echo "$reg_name" | tr -cs 'A-Za-z0-9._+' '_' )"
  rundir="${OUTROOT}/${reg_slug}"

  if [[ -e "$rundir" ]]; then
    echo "[!] Refusing to overwrite existing directory: $rundir" >&2
    exit 2
  fi

  mkdir -p "$rundir"
  cfg="${rundir}/exp.yaml"
  log="${rundir}/run.log"

  # Patch base exp.yaml -> regime exp.yaml
  python3 - "$BASE_CFG" "$cfg" "$rundir" "$reg_json" <<'PY'
import sys, yaml, json, os

src, dst, rundir = sys.argv[1], sys.argv[2], sys.argv[3]
reg = json.loads(sys.argv[4])

def die(msg): raise SystemExit(msg)

if "name" not in reg: die("regime missing name")
if "net" not in reg or not isinstance(reg["net"], dict): die("regime missing net{}")
if "qdisc" not in reg or not isinstance(reg["qdisc"], dict): die("regime missing qdisc{}")
if "flows" not in reg or not isinstance(reg["flows"], dict): die("regime missing flows{}")

with open(src) as f:
    d = yaml.safe_load(f) or {}

# --- PATCH: net ---
d.setdefault("net", {})
for k,v in reg["net"].items():
    d["net"][k] = v

# --- PATCH: qdisc ---
d.setdefault("qdisc", {})
for k,v in reg["qdisc"].items():
    d["qdisc"][k] = v

# --- PATCH: flows (exact override) ---
d["flows"] = dict(reg["flows"])

# --- PATCH: logging dir ---
d.setdefault("logging", {})
d["logging"]["dir"] = os.path.join(os.path.abspath(rundir), "tmp")

# Metadata (optional)
d["regime_name"] = reg["name"]

with open(dst, "w") as f:
    yaml.safe_dump(d, f, sort_keys=False)
PY

  # print sanity summary
  readarray -t meta < <(python3 - <<PY
import json
r=json.loads('''$reg_json''')
print(r["name"])
print(r["net"].get("rtt_ms",""))
print(r["qdisc"].get("rate",""))
print(r["qdisc"].get("limit",""))
classic = r["flows"].get("classic")
l4s = r["flows"].get("l4s")
print("classic=" + ("on" if isinstance(classic, dict) else "off"))
print("l4s=" + ("on" if isinstance(l4s, dict) else "off"))
PY
)

  echo
  echo "=================================================="
  echo "[*] ($i/$total)  ${meta[0]}"
  echo "[*] rtt_ms       ${meta[1]}"
  echo "[*] qdisc.rate   ${meta[2]}"
  echo "[*] qdisc.limit  ${meta[3]}"
  echo "[*] flows        ${meta[4]}  ${meta[5]}"
  echo "[*] dir          $rundir"
  echo "[*] cfg          $cfg"
  echo "=================================================="
  echo

  ( set -x; sudo bash "$RUN_SCRIPT" "$cfg" ) 2>&1 | tee "$log"
done

echo
echo "[✓] All regimes completed."
