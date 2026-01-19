**mahimahi_validation**

### Folder Structure**

mahimahi_validation/
│
├── dashboard/
│   ├── main.py                    # Main validation console
│   ├── validation_steps/          # Parser + permutation test
│   └── validation_visuals/        # Clustering + visual tools
│
├── mahimahi_logs/                 # Mahimahi output_*.txt
├── qdisc_logs/                    # Linux qdisc_*.log
│
├── scripts/
│   ├── mahimahi/
│   │   ├── single/
│   │   │   ├── setup.sh           # REQUIRED: CPU shielding, cset, env prep
│   │   │   ├── config.yaml        # Experiment parameters
│   │   │   └── run.sh             # Executes single-flow Mahimahi experiment
│   │   │
│   │   └── duo/
│   │       ├── setup.sh           # REQUIRED: CPU shielding, cset, env prep
│   │       ├── config.yaml.       
│   │       └── run.sh             # Executes dual-flow Mahimahi experiment
│   │
│   └── kernel/
│       ├── single/
│       │   ├── setup.sh           # REQUIRED: qdisc + namespace + CPU setup
│       │   ├── config.yaml
│       │   └── run.sh             # Executes single-flow kernel/qdisc experiment
│       │
│       └── duo/
│           ├── setup.sh           # REQUIRED: qdisc + namespace + CPU setup
│           ├── config.yaml
│           └── run.sh             # Executes dual-flow kernel/qdisc experiment
│
└── README.md

### Running the Experiment

**MahiMahi**

Run The chmod then ./setup.sh first before running.

Specify flow parameters and CPU isolation settings in the YAML configuration file.

!NOTE  
- Your CPU must have **at least 4 threads** for **classic-only** experiments and **at least 6 threads** for **dual-flow** experiments.  
- Any cores may be selected, but **do not choose cores running critical system tasks**.  
- CPU shielding will **evict all existing tasks** from the selected cores; the experiment will **error if tasks cannot be migrated** (e.g., pinned kernel threads or insufficient housekeeping cores).

Lastly, run the SHIELD_SINGLE.sh or SHIELD_DUO.sh. Remember, the flows stack based on the next missing number. So if you already have output_20, the next one will be output_21. 

**Linux Kernel**

Validation pipeline for comparing Linux kernel DualPI2 vs. Mahimahi's DualPI2.

*This tool performs:*

DTW-based similarity analysis
Queue-behavior clustering
Permutation-based statistical equivalence testing
Visualizations (histograms, CDFs, clustering plots)

**STEP 1 — Drag in your Data**
Place Mahimahi results into:
mahimahi_logs/
Place Linux kernel qdisc logs into:
qdisc_logs/
These folders ignore contents but remain in git.

**STEP 2 — Start the Validation Console**
cd dashboard
python3 main.py
You will see:
===============================
    DTW Analyzer Console
===============================
Current DTW Mode: BYTES
-------------------------------
1.  Run Parser
2.  View Histograms
3.  View CDFs
4.  Run Cluster
5.  View Cluster Summary
6.  Visualize Cluster
7.  Run Parametric Test
8.  View Parametric Test Results
9.  View Parametric Test Graph
10. Exit

--------- MODE SWITCH ---------
11. Switch to PACKETS mode
12. Switch to BYTES mode
===============================

Option Details
1. Run Parser
Reads logs, extracts queue series, computes DTW distances, saves cache.
2. View Histograms
Shows histogram of:

Kernel internal DTWs
Mahimahi internal DTWs
Cross Mahimahi↔Kernel DTWs

3. View CDFs
Shows cumulative distribution functions of the same sets.
4. Run Cluster
Runs K-Medoids + MDS visualization on all DTW distances.
5. View Cluster Summary
Prints cluster membership and medoids.
6. Visualize Cluster
Plots the embedding colored by clusters.
7. Run Parametric Test
Permutation test to measure whether Mahimahi and Kernel mean DTW differences are statistically significant.
Asks:

Number of shuffles
Epsilon tolerance (acceptable difference)

8. View Parametric Test Results
Prints the previously computed values.
9. View Parametric Test Graph
Plots histogram & CDF of shuffled DTW means + ε-corrected threshold.

Interpreting Epsilon (ε)
ε defines what mean DTW difference counts as "no meaningful difference".

BYTES MODE: ε ≈ average allowed bytes difference per sample
PACKETS MODE: ε ≈ average allowed packets difference per sample
Small ε = strict equivalence test
Large ε = tolerant equivalence test


Gitignore Behavior
mahimahi_logs/*
!mahimahi_logs/.gitkeep

qdisc_logs/*
!qdisc_logs/.gitkeep
This keeps folders tracked but ignores log contents.

Purpose
A reproducible, statistically validated pipeline to assess whether Mahimahi's DualPI2 queue behavior matches Linux kernel DualPI2.
This includes:

Queue trace comparison
Shape matching (DTW)
Cluster stability
Permutation equivalence testing

Useful for L4S, congestion control research, and simulation validation.