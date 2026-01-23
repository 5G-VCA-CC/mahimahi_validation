from validation_steps.parser import DTWAnalyzer
from validation_visuals.cluster import Cluster_DTW
from validation_steps.p_test import ParametricDTWTest
import os
from pathlib import Path

# Directories
QDISC_DIR = "../qdisc_logs"
MAHI_DIR  = "../mahimahi_logs"
CACHE_FILE = "./dtw_cache.txt"

# Global state
cluster_obj = None
test_obj = None
test_results = None
DTW_MODE = "bytes"  # default

# =============================================================
# MODULE 1 — PACKETS-IN-QUEUE VALIDATION (Your full DTW pipeline)
# =============================================================

def run_parser():
    global DTW_MODE

    print(f"\n=== Running DTW Parser (mode = {DTW_MODE}) ===")
    analyzer = DTWAnalyzer(QDISC_DIR, MAHI_DIR, CACHE_FILE, mode=DTW_MODE)

    analyzer.compute_cross()
    analyzer.compute_qdisc_internal()
    analyzer.compute_mahi_internal()
    analyzer.save_cache()

    print("DTW Parsing Complete.")


def build_full_dist(analyzer):
    full = {}

    def merge(src):
        for a, row in src.items():
            for b, d in row.items():
                full.setdefault(a, {})[b] = d

    merge(analyzer.qdisc_dist)
    merge(analyzer.mahi_dist)
    merge(analyzer.cross_dist)

    return full

# ============================================================
# FIGURE OUTPUT DIRECTORY
# ============================================================

OUT_DIR = Path("./figs")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def _save_fig(fig, name: str):
    path = OUT_DIR / name
    fig.savefig(path, dpi=200, bbox_inches="tight")
    print(f"Saved: {path.resolve()}")

# =============================================================
# GRAPHING AND ANALYSIS FUNCTIONS
# =============================================================

def view_histograms():
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, CACHE_FILE, mode=DTW_MODE)
    an.load_cache()
    fig = an.plot_triple_hist()
    _save_fig(fig, f"triple_hist_{DTW_MODE}.png")

def view_cdfs():
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, CACHE_FILE, mode=DTW_MODE)
    an.load_cache()
    fig = an.plot_triple_cdf()
    _save_fig(fig, f"triple_cdf_{DTW_MODE}.png")

def plot_graph_check():
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, CACHE_FILE, mode=DTW_MODE)
    print("QDISC_DIR resolved to:", Path(QDISC_DIR).resolve())
    print("Matched qdisc files:")
    for p in sorted(Path(QDISC_DIR).glob("qdisc_*")):
        print("  ", p.name)
    fig = an.plot_overlay_queue_traces(dt_ms=16, cutoff_ms=1000, show_legend=False)
    _save_fig(fig, f"overlay_{DTW_MODE}.png")

def run_cluster():
    global cluster_obj, DTW_MODE

    print("\n=== Running DTW Clustering ===")
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, CACHE_FILE, mode=DTW_MODE)
    an.load_cache()

    full = build_full_dist(an)
    cluster_obj = Cluster_DTW(full)

    print("Clustering complete. You may now view summary or visualization.")


def view_cluster_summary():
    if cluster_obj is None:
        print("Run clustering first.")
        return
    cluster_obj.summary()


def view_cluster_visual():
    if cluster_obj is None:
        print("Run clustering first.")
        return
    cluster_obj.visualize()


def run_parametric_test():
    global test_obj, test_results, DTW_MODE

    print("\n=== Running Parametric Permutation Test ===")

    # Load DTW distances
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, CACHE_FILE, mode=DTW_MODE)
    an.load_cache()

    mahi_keys = list(an.mahi_dist.keys())
    qdisc_keys = list(an.qdisc_dist.keys())
    full = build_full_dist(an)

    # Construct test object
    test_obj = ParametricDTWTest(full, mahi_keys, qdisc_keys)

    # → Number of permutations
    num = input("How many random permutations? [default = 200]: ").strip()
    num = int(num) if num else 200

    # → Ask for epsilon
    print("\n--- Equivalence Threshold Setup ---")
    print("This epsilon is the acceptable DTW mean-difference considered negligible.")
    print("Defines the maximum difference where Mahi and Qdisc are considered equivalent.")
    print("Smaller epsilon → stricter. Larger epsilon → more tolerant.")
    eps_in = input("Enter epsilon (acceptable DTW mean difference): ").strip()

    try:
        epsilon = float(eps_in)
    except:
        print("Invalid. Using epsilon = 1.0.")
        epsilon = 1.0

    # Run test
    test_results = test_obj.run(num_shuffles=num, epsilon=epsilon)

    print("\n=== Permutation Test Finished ===")
    print(f"Original M-Q Mean DTW = {test_results['original_mean']:.6f}")
    print(f"Num random ≥ original = {test_results['num_greater']}/{num}")
    print(f"p-value = {test_results['p_value']:.6f}\n")

def view_parametric_results():
    if test_results is None:
        print("Run parametric test first.")
        return

    print("\n=== Permutation Test Results ===")
    print(f"Original Mean: {test_results['original_mean']:.6f}")
    print(f"Num greater: {test_results['num_greater']}")
    print(f"p-value: {test_results['p_value']:.6f}")


def view_parametric_graph():
    if test_obj is None or test_results is None:
        print("Run the parametric test first.")
        return
    test_obj.plot(show=False, save_path=f"./figs/perm_{DTW_MODE}.png")


def set_mode_packets():
    global DTW_MODE
    DTW_MODE = "packets"
    print("Switched DTW mode → PACKETS")


def set_mode_bytes():
    global DTW_MODE
    DTW_MODE = "bytes"
    print("Switched DTW mode → BYTES")


# =============================================================
# MODULE 1 SUBMENU (Packets-in-Queue Validation)
# =============================================================

def packets_in_queue_menu():
    while True:
        print("\n=========== Packets-in-Queue Validation ===========")
        print(f"Current DTW Mode: {DTW_MODE.upper()}")
        print("1. Run Parser")
        print("2. View Histograms")
        print("3. View CDFs")
        print("4. Run Clustering")
        print("5. View Cluster Summary")
        print("6. Visualize Cluster")
        print("7. Run Parametric Test")
        print("8. View Parametric Test Results")
        print("9. View Parametric Test Graph")
        print("10. Plot Graph Check (Mahimahi vs Kernel overlay)")  # ✅ NEW
        print("------ MODE SWITCH ------")
        print("11. Switch to PACKETS mode")
        print("12. Switch to BYTES mode")
        print("0. Back to Main Menu")
        print("====================================================")

        choice = input("Enter choice: ").strip()

        if choice == "1": run_parser()
        elif choice == "2": view_histograms()
        elif choice == "3": view_cdfs()
        elif choice == "4": run_cluster()
        elif choice == "5": view_cluster_summary()
        elif choice == "6": view_cluster_visual()
        elif choice == "7": run_parametric_test()
        elif choice == "8": view_parametric_results()
        elif choice == "9": view_parametric_graph()
        elif choice == "10": plot_graph_check()  # ✅ NEW
        elif choice == "11": set_mode_packets()
        elif choice == "12": set_mode_bytes()
        elif choice == "0":
            return
        else:
            print("Invalid choice.")


# =============================================================
# MODULE 2 — DROP PROBABILITY VALIDATION (Placeholder)
# =============================================================

def drop_probability_menu():
    while True:
        print("\n=========== Drop Probability Validation ===========")
        print("1. Parse drop probability logs")
        print("2. Plot drop probability over time")
        print("3. Compare Mahimahi vs Kernel")
        print("0. Back to Main Menu")
        print("===================================================")

        choice = input("Enter choice: ").strip()

        if choice == "1":
            print("(TODO) Drop probability parser not implemented yet.")
        elif choice == "2":
            print("(TODO) Plot drop probability not implemented yet.")
        elif choice == "3":
            print("(TODO) Drop probability comparison not implemented yet.")
        elif choice == "0":
            return
        else:
            print("Invalid choice.")


# =============================================================
# MODULE 3 — PACKETS DROPPED VALIDATION (Placeholder)
# =============================================================

def packets_dropped_menu():
    while True:
        print("\n=========== Packets Dropped Validation ===========")
        print("1. Parse drop logs")
        print("2. Plot packets dropped over time")
        print("3. Compare Mahimahi vs Kernel")
        print("0. Back to Main Menu")
        print("==================================================")

        choice = input("Enter choice: ").strip()

        if choice == "1":
            print("(TODO) Drop count parser not implemented yet.")
        elif choice == "2":
            print("(TODO) Plot dropped packets not implemented yet.")
        elif choice == "3":
            print("(TODO) Comparison not implemented yet.")
        elif choice == "0":
            return
        else:
            print("Invalid choice.")


# =============================================================
# OUTER VALIDATION CONSOLE (Top-level)
# =============================================================

def main_menu():
    while True:
        print("\n===============================")
        print("        Validation Console")
        print("===============================")
        print("1. Packets-in-Queue Validation (DTW based)")
        print("2. Drop Probability Validation")
        print("3. Packets Dropped Validation")
        print("0. Exit")
        print("===============================")

        choice = input("Enter choice: ").strip()

        if choice == "1": packets_in_queue_menu()
        elif choice == "2": drop_probability_menu()
        elif choice == "3": packets_dropped_menu()
        elif choice == "0":
            print("Exiting.")
            break
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main_menu()