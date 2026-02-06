from validation_steps.parser import DTWAnalyzer
from validation_steps.p_test import NonparametricDTWTest
from pathlib import Path

# ============================================================
# Validation Tool for AQM statuses
# ============================================================

# Directories
QDISC_DIR = "../qdisc_logs"
MAHI_DIR  = "../mahimahi_logs"

# Output directory
OUT_DIR = Path("./figs")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def _save_fig(fig, name: str):
    path = OUT_DIR / name
    fig.savefig(path, dpi=200, bbox_inches="tight")
    print(f"Saved: {path.resolve()}")

# Global state
test_obj = None
test_results = None

# DTW variable mode
DTW_MODE = "packets"   # packets | bytes | ecn_mark | t_ms | drops

# =============================================================
# Parser / Pipeline
# =============================================================

def run_parser():
    global DTW_MODE
    print(f"\n=== Running DTW Parser (variable = {DTW_MODE}) ===")

    analyzer = DTWAnalyzer(QDISC_DIR, MAHI_DIR, mode=DTW_MODE)

    analyzer.compute_cross()
    analyzer.compute_qdisc_internal()
    analyzer.compute_mahi_internal()

    print("Saving cache...")
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

# =============================================================
# Visualization helpers
# =============================================================

def view_histograms():
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, mode=DTW_MODE)
    an.load_cache()
    fig = an.plot_triple_hist()
    _save_fig(fig, f"triple_hist_{DTW_MODE}.png")

def view_cdfs():
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, mode=DTW_MODE)
    an.load_cache()
    fig = an.plot_triple_cdf()
    _save_fig(fig, f"triple_cdf_{DTW_MODE}.png")

def plot_graph_check():
    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, mode=DTW_MODE)

    print("QDISC_DIR resolved to:", Path(QDISC_DIR).resolve())
    print("Matched qdisc files:")
    for p in sorted(Path(QDISC_DIR).glob("qdisc_*")):
        print("  ", p.name)

    fig = an.plot_overlay_queue_traces(
        cutoff_ms=0,
    )
    _save_fig(fig, f"overlay_{DTW_MODE}.png")

# =============================================================
# Nonparametric permutation test
# =============================================================

def run_nonparametric_test():
    global test_obj, test_results, DTW_MODE

    print(f"\n=== Running NONPARAMETRIC Permutation Test (variable = {DTW_MODE}) ===")

    an = DTWAnalyzer(QDISC_DIR, MAHI_DIR, mode=DTW_MODE)
    an.load_cache()

    mahi_keys  = list(an.mahi_dist.keys())
    qdisc_keys = list(an.qdisc_dist.keys())

    full = build_full_dist(an)

    test_obj = NonparametricDTWTest(full, mahi_keys, qdisc_keys)

    num = input("How many random permutations? [default = 20,000]: ").strip()
    num = int(num) if num else 20000

    print("\n--- Equivalence Threshold Setup ---")
    print("epsilon = acceptable DTW mean difference considered negligible.")

    eps_in = input("Enter epsilon: ").strip()
    try:
        epsilon = float(eps_in)
    except:
        epsilon = 0

    test_results = test_obj.run(
        num_shuffles=num,
        epsilon=epsilon
    )

    print("\n=== Permutation Test Finished ===")
    print(f"Observed M–Q Mean DTW = {test_results['original_mean']:.6f}")
    print(f"Num random ≥ observed = {test_results['num_ge']}/{num}")
    print(f"p-value = {test_results['p_value']:.6f}\n")

def view_nonparametric_results():
    if test_results is None:
        print("Run the nonparametric test first.")
        return

    print("\n=== Nonparametric Permutation Test Results ===")
    print(f"Observed Mean: {test_results['original_mean']:.6f}")

    # Prefer new key, fall back to old for safety
    num_ge = test_results.get("num_ge", test_results.get("num_greater"))
    total = test_results.get("num_shuffles_used",
                              test_results.get("num_shuffles_requested"))

    print(f"Num shuffled ≥ observed: {num_ge}/{total}")
    print(f"p-value: {test_results['p_value']:.6f}")

    # Optional: quick interpretation (correct direction)
    if test_results["p_value"] < 0.05:
        print("→ Evidence of DIFFERENCE (observed DTW unusually large under shuffling).")
    else:
        print("→ No strong evidence of difference.")

def view_nonparametric_graph():
    if test_obj is None or test_results is None:
        print("Run the nonparametric test first.")
        return

    test_obj.plot(
        show=False,
        save_path=f"./figs/perm_{DTW_MODE}.png"
    )

# =============================================================
# Mode switches
# =============================================================

def set_mode_packets():
    global DTW_MODE
    DTW_MODE = "packets"
    print("Switched variable → PACKETS (q_pkts)")

def set_mode_bytes():
    global DTW_MODE
    DTW_MODE = "bytes"
    print("Switched variable → BYTES (q_bytes)")

def set_mode_ecn():
    global DTW_MODE
    DTW_MODE = "ecn_mark"
    print("Switched variable → ECN_MARK (ecn_mark)")

def set_mode_time():
    global DTW_MODE
    DTW_MODE = "t_ms"
    print("Switched variable → TIME (t_ms)")

def set_mode_packet_dropped_total():
    global DTW_MODE
    DTW_MODE = "packet_dropped_total"
    print("Switched variable → PACKET_DROPPED_TOTAL")

def set_mode_packet_dropped_l4s():
    global DTW_MODE
    DTW_MODE = "packet_dropped_l4s"
    print("Switched variable → PACKET_DROPPED_L4S")

def set_mode_packet_dropped_classic():
    global DTW_MODE
    DTW_MODE = "packet_dropped_classic"
    print("Switched variable → PACKET_DROPPED_CLASSIC")

# =============================================================
# Menu
# =============================================================

def aqm_status_menu():
    while True:
        print("\n===============================")
        print("   Validation Tool for AQM statuses")
        print("===============================")
        print(f"Current variable: {DTW_MODE.upper()}")
        print("")
        print("1. Run Parser (compute DTW + save cache)")
        print("2. View Histograms")
        print("3. View CDFs")
        print("4. Plot Graph Check (Mahimahi vs Kernel overlay)")
        print("5. Run NONPARAMETRIC Permutation Test")
        print("6. View Permutation Test Results")
        print("7. View Permutation Test Graph")
        print("------ VARIABLE SWITCH ------")
        print("8.  Use PACKETS (q_pkts)")
        print("9.  Use BYTES (q_bytes)")
        print("10. Use ECN_MARK (ecn_mark)")
        print("11. Use TIME (t_ms)")
        print("12. Use PACKET_DROPPED_TOTAL")
        print("13. Use PACKET_DROPPED_L4S")
        print("14. Use PACKET_DROPPED_CLASSIC")
        print("0. Exit")
        print("===============================")

        choice = input("Enter choice: ").strip()

        if choice == "1": run_parser()
        elif choice == "2": view_histograms()
        elif choice == "3": view_cdfs()
        elif choice == "4": plot_graph_check()
        elif choice == "5": run_nonparametric_test()
        elif choice == "6": view_nonparametric_results()
        elif choice == "7": view_nonparametric_graph()
        elif choice == "8": set_mode_packets()
        elif choice == "9": set_mode_bytes()
        elif choice == "10": set_mode_ecn()
        elif choice == "11": set_mode_time()
        elif choice == "12": set_mode_packet_dropped_total()
        elif choice == "13": set_mode_packet_dropped_l4s()
        elif choice == "14": set_mode_packet_dropped_classic()
        elif choice == "0":
            print("Exiting.")
            return
        else:
            print("Invalid choice.")

if __name__ == "__main__":
    # >>> CHANGED: nothing required here; derivative behavior lives in parser.py
    aqm_status_menu()
    # <<< CHANGED
