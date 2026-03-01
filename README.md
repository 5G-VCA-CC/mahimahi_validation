# Mahimahi ↔ Linux qdisc Validation Framework

This repository provides a **fully automated validation pipeline** for comparing  
**Mahimahi-based network emulation** against **native Linux kernel qdisc behavior**.

It is designed to answer, rigorously and reproducibly:

- Does Mahimahi statistically match kernel behavior?
- If not perfectly identical, how often does it exceed natural within-system variability?
- How confident are we in that estimate given a finite number of runs?
- Which tuning (threshold/target variants) best matches kernel behavior?

---

> [!NOTE]  
> If you only want to validate the data we have already collected, simply run the scripts as provided.  
> No modifications are required — they already point to the correct data directories included in this repository.

Purpose
- This folder is ONLY for validating the data we already collected.
- You do NOT need to run new experiments.
- All raw experiment outputs are already attached in this repository.
- The raw data originally came from:
  https://github.com/5G-VCA-CC/Experimentation-Data-Scripts
- Everything needed to reproduce the validation plots is already included here.

How to use
- Go into the folder:
  1validation_scripts
- Run the scripts directly.
- No additional data collection is required.

### Overlay / Comparison Scripts

- `overlay_comparison_throughput_finale.py`  
  - Overlays throughput CDFs of the final Mahimahi configurations against the kernel DualPI2 implementation.  
  - Final Mahimahi settings used:  
    - 12 Mbps: thresh = 5 ms, target = 30 ms  
    - 50 Mbps: thresh = 5 ms, target = 30 ms  
    - 200 Mbps: thresh = 10 ms, target = 45 ms  
  - Parses iperf receiver average bitrate and plots CDF overlays.

- `overlay_throughput_cdf_target.py`  
  - Compares different target values (15–30 ms range) for each bandwidth scenario.  
  - Overlays Mahimahi 12 Mbps vs kernel 12 Mbps, 50 Mbps vs kernel 50 Mbps, and 200 Mbps vs kernel 200 Mbps.  
  - Used to study sensitivity to target changes.

- `overlay_throughput_cdf_thresh.py`  
  - Compares different threshold values while holding target fixed.  
  - Shows how varying thresh affects throughput relative to the kernel baseline.  
  - Produces per-bandwidth overlays (12 / 50 / 200 Mbps).

### Verification / CI Scripts

- `verify_finale.py`  
  - Computes p_hat_max for the final Mahimahi configuration relative to the kernel DualPI2 reference.  
  - Generates the validation histogram for the final configuration.

- `verify_original.py`  
  - Computes p_hat_max for the original configuration (target = 15 ms, thresh = 1 ms) relative to the kernel DualPI2 reference.  
  - Generates the validation histogram for the baseline setup.

- `ci.py`  
  - Prints bootstrap confidence intervals for p_hat_max (original and final configurations).  
  - Plots CI width vs number of runs to verify that the sample size is sufficient and that the interval stabilizes.

WHAT THIS REPOSITORY PROVIDES

1. Automated Log Parsing

The framework automatically parses:

- Mahimahi logs (output_*)
- Linux kernel qdisc logs (qdisc_*)

It extracts:

- Time-series metrics:
  - Packets
  - ECN marks
  - Queue delay
  - Dropped packets
  - Other per-interval counters
- Scalar metrics:
  - iperf totals
  - Average queue delay per run

For time-series metrics, the tool computes Dynamic Time Warping (DTW) distances:

- Within-qdisc variability
- Within-Mahimahi variability
- Cross Mahimahi ↔ qdisc distances

All DTW results are cached to allow fast reuse without recomputation.


2. Statistical Validation Framework (Core Contribution)

This framework does not rely on visual similarity alone. It provides formal statistical validation.

Within-System Variability Envelope

From the within-qdisc and within-Mahimahi DTW distributions, we compute:

- q95_qdisc
- q95_mahimahi

Then define:

- eps_min = min(q95_qdisc, q95_mahimahi)
- eps_max = max(q95_qdisc, q95_mahimahi)  (conservative envelope)

This defines the natural variability region of each system.

Exceedance Probability

For cross-system distances:

p_max = P(d_cross > eps_max)

This quantifies:

How often Mahimahi exceeds conservative within-system variability.

Bootstrap Confidence Intervals (Run-ID Resampling)

We compute bootstrap confidence intervals for p_max using run-ID resampling
(not raw pairwise distances, to preserve dependence structure).

Outputs include:

- Two-sided confidence interval [p_lo, p_hi]
- One-sided upper bound

This quantifies uncertainty due to finite runs.

Nonparametric Permutation Test

We provide a permutation test that:

- Randomly reshuffles labels
- Computes mean DTW under random assignments
- Compares to observed Mahimahi ↔ qdisc mean

Outputs:

- Observed mean
- p-value
- Optional convergence diagnostics

CI Width vs Number of Runs

We provide a diagnostic plot of bootstrap CI width as a function of run count.

This helps justify whether 100 runs (or any chosen n) is sufficient by showing stabilization behavior.


OUTPUT PLOTS

The framework generates:

1. Triple Histogram
- Within-qdisc
- Within-Mahimahi
- Cross
- Includes q95 thresholds and p_min / p_max annotations

2. Validation Histogram (Paper Plot)
Cross-only histogram showing:

- eps_min
- eps_max
- p_min
- p_max

3. Triple CDF (DTW-only)

4. Overlay Time-Series Check (DTW-only)

5. CI Width vs n


CORE INTERFACE: AQMValidationTool

The central class powering the framework is AQMValidationTool.

It supports:

- Running the DTW parser with caching
- Histogram generation
- CDF generation
- Overlay plotting
- Bootstrap CI computation
- CI vs n diagnostics
- Permutation testing
- Scalar validation modes
- Mode switching

It can be used:

- Interactively via menu
- Programmatically in scripts
- Fully headless in batch validation


HEADLESS VALIDATION RUNNER

A headless validation runner is included to execute the full pipeline automatically
for multiple bandwidth scenarios:

- 12 Mbps
- 50 Mbps
- 200 Mbps

Without menu interaction.

It performs:

1. Parsing
2. Histogram generation
3. Throughput validation
4. ECN validation
5. Packet-drop validation

All outputs are written into structured subfolders.


1validation_scripts Directory

In addition to the core validation tool, the repository includes
a 1validation_scripts directory containing helper scripts used for:

- Paper-quality CDF overlays
- Tuning comparison sweeps
- Original vs final dataset verification

These scripts do not compute DTW. They generate overlay comparisons
for throughput and queue delay across tuning configurations.


Overlay CDF Scripts

overlay_comparison_throughput_cdf.py

Generates 3-panel CDF plots of throughput (iperf) across:

- 12 Mbps
- 50 Mbps
- 200 Mbps

Compares:

- Mahimahi
- Kernel (qdisc)

Used for BDP sweep comparisons.


overlay_comparison_throughput_finale.py

Generates 3-panel CDF plots of throughput (iperf) across:

- 12 Mbps
- 50 Mbps
- 200 Mbps

Compares:

- Final tuned Mahimahi configuration
- Kernel (qdisc)

Used for final reporting comparisons.


overlay_qdelay_cdf.py

Generates queue delay CDF overlays across:

- Baseline Mahimahi
- Tuned target-based variants
- Kernel qdisc

Used for target-based tuning evaluation.


overlay_throughput_cdf_target.py

Throughput CDF comparison for target-based Mahimahi variants
against kernel qdisc.


overlay_throughput_cdf_thresh.py

Throughput CDF comparison for threshold-based tuning sweeps
against kernel qdisc.


Verification Scripts

verify_original.py

Runs the full validation pipeline on the original dataset selection.

"Original" refers to:

- The earlier experiment set
- Before tuning refinements
- Before improved threshold/target selection

verify_finale.py

Runs validation on the final tuned dataset selection.

"Finale" refers to:

- The final chosen configuration
- The set determined to be best validated
- The configuration used for final reporting


Directory Conventions

Expected input naming:

- Mahimahi logs: output_*
- qdisc logs: qdisc_*

Expected directory structure:

qdisc/
  12mbps/
  50mbps/
  200mbps/

mahimahi/
  12mbps/
  50mbps/
  200mbps/


Design Philosophy

This framework ensures:

- Reproducibility
- Statistical rigor
- No reliance on visual-only comparisons
- Clear separation between:
  - Within-system variability
  - Cross-system discrepancy
  - Statistical confidence

It enables principled statements such as:

"The cross-system exceedance rate is below 5% with 95% confidence."

rather than relying on visual similarity claims.


Summary

This repository provides:

- Automated Mahimahi vs kernel comparison
- DTW-based time-series validation
- Scalar metric validation
- Bootstrap confidence intervals
- Nonparametric permutation testing
- Batch experiment validation
- Paper-quality overlay visualizations
- Original vs final tuning verification

It is a complete statistical validation framework for evaluating
Mahimahi emulation fidelity against Linux qdisc behavior.