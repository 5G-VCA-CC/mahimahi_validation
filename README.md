# Mahimahi ↔ Linux qdisc Validation Framework

This repository provides a **fully automated validation pipeline** for comparing  
**Mahimahi-based network emulation** against **native Linux kernel qdisc behavior**.

It is designed to answer, rigorously and reproducibly:

- Does Mahimahi statistically match kernel behavior?
- If not perfectly identical, how often does it exceed natural within-system variability?
- How confident are we in that estimate given a finite number of runs?
- Which tuning (threshold/target variants) best matches kernel behavior?

This is not just plotting — it is a **statistically grounded validation system**.

---

# What This Repository Provides

## 1️⃣ Automated Log Parsing

The tool:

- Parses Mahimahi logs (`output_*`)
- Parses Linux qdisc logs (`qdisc_*`)
- Extracts:
  - Time-series metrics (packets, ECN marks, queue delay, dropped packets, etc.)
  - Scalar metrics (iperf totals, average queue delay per run)

For time-series metrics, it computes **DTW (Dynamic Time Warping)** distances:
- Within-qdisc variability
- Within-Mahimahi variability
- Cross Mahimahi ↔ qdisc distances

Results are cached for fast reuse.

---

## 2️⃣ Statistical Validation (Core Contribution)

We do not rely on visual similarity alone. The framework provides:

### ✔ Within-System Variability Envelope

From within-qdisc and within-Mahimahi DTW distributions, we compute:

- `q95_qdisc`
- `q95_mahimahi`

Then define:

- `eps_min = min(q95_qdisc, q95_mahi)`
- `eps_max = max(q95_qdisc, q95_mahi)`  ← conservative envelope

This defines the “natural variability region.”

---

### ✔ Exceedance Probability

For cross distances:

\[
p_{\max} = P(d_{cross} > \varepsilon_{\max})
\]

This answers:

> How often does Mahimahi exceed conservative within-system variability?

---

### ✔ Bootstrap Confidence Intervals (Run-ID Resampling)

We compute a bootstrap CI for `p_max` using **run-ID resampling**  
(not raw pairwise distances — this preserves dependence structure).

Outputs:
- Two-sided CI `[p_lo, p_hi]`
- One-sided upper bound

This quantifies uncertainty due to finite runs.

---

### ✔ Nonparametric Permutation Test

We also provide a permutation test:

- Randomly reshuffle labels
- Compute mean DTW under random assignments
- Compare to observed Mahimahi ↔ qdisc mean

Outputs:
- Observed mean
- p-value
- Optional convergence plot

---

### ✔ CI Width vs n

We provide a diagnostic:

- Bootstrap CI width as a function of run count
- Helps justify why 100 runs (or any n) is sufficient

---

# Output Plots

The framework generates:

### 1️⃣ Triple Histogram
- Within-qdisc
- Within-Mahimahi
- Cross
- Includes q95 thresholds and p_min / p_max

File:

triple_hist_<mode>.svg


---

### 2️⃣ Validation Histogram (Paper Plot)
Cross-only histogram showing:

- eps_min
- eps_max
- p_min
- p_max

File:

paper-graphs-<bandwidth>-reject_hist_<mode>.svg


---

### 3️⃣ Triple CDF (DTW-only)

triple_cdf_<mode>.svg


---

### 4️⃣ Overlay Time-Series Check (DTW-only)

overlay_<mode>.svg


---

### 5️⃣ CI Width vs n

<mode>_ci_width_vs_n.svg


---

# Core Interface: `AQMValidationTool`

This is the main class that powers everything.

It supports:

- Running DTW parser + caching
- Histogram generation
- CDF generation
- Overlay plotting
- Bootstrap CI printing
- CI vs n diagnostics
- Permutation test execution
- Scalar validation modes
- Mode switching

It can be used:

- Interactively via menu
- Programmatically in scripts
- Fully headless in batch validation

---

# Headless Validation Runner (Used for Final Validation)

We provide a script that runs the full validation pipeline automatically for multiple bandwidth pairs:

- 12 Mbps
- 50 Mbps
- 200 Mbps

Without menu interaction.

This is what we used to validate our kernel vs Mahimahi experiments.

It:
1. Runs parser
2. Generates histograms
3. Generates throughput validation
4. Generates ECN validation
5. Generates packet-drop validation

All outputs are written into structured subfolders.

---

# `1validation_scripts/` Directory

In addition to the core validation tool, this repository includes  
a `1validation_scripts/` directory containing helper scripts used for:

- Paper-quality CDF overlays
- Tuning comparison sweeps
- Original vs final dataset verification

These scripts do not compute DTW — they generate comparison overlays  
for throughput and queue delay across tuning configurations.

---

## Overlay CDF Scripts

### `overlay_comparison_throughput_cdf.py`

Generates 3-panel CDF plots of **throughput (iperf)** across:

- 12 Mbps
- 50 Mbps
- 200 Mbps

Compares:
- Mahimahi
- Kernel (qdisc)

Outputs:

figs_overlay/<name>_cdf_bdp12.svg
figs_overlay/<name>_cdf_bdp50.svg
figs_overlay/<name>_cdf_bdp200.svg


Used for BDP sweep comparisons.

---

### `overlay_qdelay_cdf.py`

Generates CDF overlays for queue delay (e.g., `qdelay_c`) across:

- Baseline Mahimahi
- Tuned target-30ms variant
- Tuned target-45ms variant
- Kernel qdisc

Across 12 / 50 / 200 Mbps.

Used for **target-based tuning evaluation**.

---

### `overlay_throughput_cdf_target.py`

Throughput CDF comparison for **target-based Mahimahi variants**  
against kernel qdisc.

---

### `overlay_throughput_cdf_thresh.py`

Throughput CDF comparison for **threshold-based tuning sweeps**  
(e.g., L4S threshold variants) against kernel qdisc.

---

# Verification Scripts

These are batch runners tied to specific experiment sets.

---

## `verify_original.py`

Runs the full validation pipeline on the **original dataset selection**.

“Original” refers to:

- The earlier experiment set
- Before later tuning refinements
- Before the improved threshold/target selection

This reflects the baseline Mahimahi configuration before final tuning.

---

## `verify_finale.py`

Runs validation on the **finale dataset selection**.

“Finale” refers to:

- The final tuned configuration
- The set we determined to be best validated
- The configuration used for final reporting

In short:

| Script | Meaning |
|--------|----------|
| `verify_original.py` | Earlier baseline validation set |
| `verify_finale.py` | Final best validated configuration |

---

# Directory Conventions

Expected input naming:

- Mahimahi logs: `output_*`
- qdisc logs: `qdisc_*`

Expected directory structure example:


qdisc/
12mbps/
50mbps/
200mbps/

mahimahi/
12mbps/
50mbps/
200mbps/


---

# Design Philosophy

This framework was built to ensure:

- Reproducibility
- Statistical rigor
- No reliance on visual-only comparison
- Clear separation of:
  - Within-system variability
  - Cross-system discrepancy
  - Statistical confidence

It enables principled statements such as:

> “The cross-system exceedance rate is below 5% with 95% confidence.”

Instead of:

> “The curves look similar.”

---

# Summary

This repository provides:

- Automated Mahimahi ↔ kernel comparison
- DTW-based time-series validation
- Scalar metric validation
- Bootstrap CI estimation
- Nonparametric permutation testing
- Batch experiment validation
- Paper-quality overlay visualizations
- Original vs final tuning verification

It is a complete statistical validation framework  
for evaluating Mahimahi emulation fidelity against Linux qdisc behavior.

---