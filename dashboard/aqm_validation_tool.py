from __future__ import annotations

from pathlib import Path
import re
import numpy as np

from validation_steps.parser import DTWAnalyzer
from validation_steps.p_test import NonparametricDTWTest
from validation_steps.min import run_iperf_totalreceived_histogram

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

class AQMValidationTool:
    """
    Interactive validation menu + callable methods.

    Members:
      - qdisc_dir
      - mahimahi_dir
    """

    # For avg-delay scalar modes (no DTW)
    _ID_AT_END = re.compile(r"_(\d+)$")
    _TICK_NS_RE = re.compile(r"^(?:TICK_NS|TS_NS)\s+(\d+)\s*$")
    _DELAY_RE = re.compile(r"\bdelay_c\s+(\d+)([a-zA-Z]+)\s+delay_l\s+(\d+)([a-zA-Z]+)\b")

    def __init__(
        self,
        qdisc_dir: str | Path = "../qdisc_logs",
        mahimahi_dir: str | Path = "../mahimahi_logs",
        out_dir: str | Path = "./figs",
        dtw_mode: str = "packets",
    ):
        self.qdisc_dir = str(qdisc_dir)
        self.mahimahi_dir = str(mahimahi_dir)

        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.dtw_mode = dtw_mode  # "packets" | ... | "iperf_totalreceived" | "l_average_queue_delay" | "c_average_queue_delay"

        self.test_obj: NonparametricDTWTest | None = None
        self.test_results: dict | None = None

    # =============================================================
    # Internals
    # =============================================================

    def _save_fig(self, fig, name: str):
        path = self.out_dir / name
        fig.savefig(path, dpi=200, bbox_inches="tight")
        print(f"Saved: {path.resolve()}")

    @staticmethod
    def _build_full_dist(analyzer: DTWAnalyzer) -> dict:
        full: dict = {}

        def merge(src):
            for a, row in src.items():
                for b, d in row.items():
                    full.setdefault(a, {})[b] = d

        merge(analyzer.qdisc_dist)
        merge(analyzer.mahi_dist)
        merge(analyzer.cross_dist)
        return full

    def _analyzer(self) -> DTWAnalyzer:
        return DTWAnalyzer(self.qdisc_dir, self.mahimahi_dir, mode=self.dtw_mode)

    # =============================================================
    # Scalar (no-DTW) helpers: average queue delay per run
    # =============================================================

    @staticmethod
    def _unit_to_ms(v: int, unit: str) -> float:
        u = unit.strip().lower()
        if u in ("us", "usec", "usecs"):
            return v / 1000.0
        if u in ("ms", "msec", "msecs"):
            return float(v)
        if u in ("ns", "nsec", "nsecs"):
            return v / 1e6
        # unknown -> treat like us
        return v / 1000.0

    def _collect_id_files(self, root: str | Path, *, glob_pattern: str = "*") -> list[tuple[int, Path]]:
        root = Path(root)
        out: list[tuple[int, Path]] = []
        for p in sorted(root.glob(glob_pattern)):
            if not p.is_file():
                continue
            m = self._ID_AT_END.search(p.stem)
            if not m:
                continue
            out.append((int(m.group(1)), p))
        return out

    def _avg_delay_ms_from_mahi_file(self, path: Path, which: str) -> float:
        # which: "l" or "c"
        key = "qdelay_l_ms=" if which == "l" else "qdelay_c_ms="

        vals = []
        try:
            for line in path.read_text(errors="ignore").splitlines():
                if not line.startswith("[QUEUE_STATS]"):
                    continue
                # quick parse: scan tokens
                for tok in line.split():
                    if tok.startswith(key):
                        v = tok[len(key):].strip().rstrip(",")
                        try:
                            vals.append(float(v))
                        except Exception:
                            pass
                        break
        except FileNotFoundError:
            return 0.0

        if not vals:
            return 0.0
        return float(np.mean(np.asarray(vals, dtype=np.float64)))

    def _avg_delay_ms_from_qdisc_file(self, path: Path, which: str) -> float:
        # which: "l" or "c"
        vals = []
        in_block = False
        block_delay_l = None
        block_delay_c = None

        def flush():
            nonlocal block_delay_l, block_delay_c
            if which == "l":
                if block_delay_l is not None:
                    vals.append(float(block_delay_l))
            else:
                if block_delay_c is not None:
                    vals.append(float(block_delay_c))

        try:
            with open(path, "r", errors="ignore") as f:
                for line in f:
                    if self._TICK_NS_RE.match(line):
                        # new tick block
                        if in_block:
                            flush()
                        in_block = True
                        block_delay_l = None
                        block_delay_c = None
                        continue

                    if not in_block:
                        continue

                    if "delay_c" in line and "delay_l" in line:
                        m = self._DELAY_RE.search(line)
                        if m:
                            c_val = int(m.group(1)); c_unit = m.group(2)
                            l_val = int(m.group(3)); l_unit = m.group(4)
                            block_delay_c = self._unit_to_ms(c_val, c_unit)
                            block_delay_l = self._unit_to_ms(l_val, l_unit)

            if in_block:
                flush()
        except FileNotFoundError:
            return 0.0

        if not vals:
            return 0.0
        return float(np.mean(np.asarray(vals, dtype=np.float64)))

    @staticmethod
    def _pairwise_abs_diffs(x: np.ndarray) -> np.ndarray:
        n = x.size
        if n < 2:
            return np.asarray([], dtype=np.float64)
        vals = []
        for i in range(n):
            for j in range(i + 1, n):
                vals.append(abs(x[i] - x[j]))
        return np.asarray(vals, dtype=np.float64)

    @staticmethod
    def _cross_abs_diffs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        if a.size == 0 or b.size == 0:
            return np.asarray([], dtype=np.float64)
        vals = []
        for ai in a:
            for bj in b:
                vals.append(abs(ai - bj))
        return np.asarray(vals, dtype=np.float64)

    def _plot_triple_hist_scalar_ms(self, q_within: np.ndarray, m_within: np.ndarray, cross: np.ndarray, *, quantile: float = 95.0):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        q_thr = np.percentile(q_within, quantile) if q_within.size else np.nan
        m_thr = np.percentile(m_within, quantile) if m_within.size else np.nan

        eps_min = min(q_thr, m_thr)
        eps_max = max(q_thr, m_thr)

        n = cross.size
        if n == 0:
            p_min = p_max = np.nan
        else:
            p_min = (np.sum(cross > eps_min) + 1) / (n + 1)
            p_max = (np.sum(cross > eps_max) + 1) / (n + 1)

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        axes[0].hist(q_within, bins=30, edgecolor="black")
        axes[0].axvline(q_thr, linestyle="--")
        axes[0].set_title(f"Qdisc |Δ| (ms)\nq{quantile:g}={q_thr:.4g}")

        axes[1].hist(m_within, bins=30, edgecolor="black")
        axes[1].axvline(m_thr, linestyle="--")
        axes[1].set_title(f"Mahimahi |Δ| (ms)\nq{quantile:g}={m_thr:.4g}")

        axes[2].hist(cross, bins=30, edgecolor="black")
        axes[2].axvline(eps_min, linestyle="--")
        axes[2].axvline(eps_max, linestyle="--")
        axes[2].set_title(
            f"Cross |Δ| (ms)\n"
            f"eps_min={eps_min:.4g}, p_min={p_min:.4f} | "
            f"eps_max={eps_max:.4g}, p_max={p_max:.4f}"
        )

        for ax in axes:
            ax.set_xlabel("|Δ avg delay| (ms)")
            ax.grid(True, alpha=0.2)
        axes[0].set_ylabel("count")

        plt.tight_layout()
        return fig

    def run_avg_queue_delay_histogram(self, which: str, *, quantile: float = 95.0):
        """
        which: "l" or "c"

        Computes per-run AVG queue delay for qdisc logs + mahimahi logs.
        Then plots:
        1) Per-run line plot (connected)
        2) Triple histogram over |Δ| distributions
        (within qdisc, within mahimahi, cross)
        """

        # Collect the correct files for each source
        q_items = self._collect_id_files(self.qdisc_dir, glob_pattern="qdisc_*")
        m_items = self._collect_id_files(self.mahimahi_dir, glob_pattern="output_*")

        # One scalar (avg delay) per file/run
        q = np.asarray(
            [self._avg_delay_ms_from_qdisc_file(p, which) for _, p in q_items],
            dtype=np.float64,
        )
        m = np.asarray(
            [self._avg_delay_ms_from_mahi_file(p, which) for _, p in m_items],
            dtype=np.float64,
        )

        if q.size == 0 or m.size == 0:
            raise RuntimeError(
                f"Need non-empty samples. qdisc={q.size}, mahi={m.size} "
                f"(patterns: qdisc_* / output_*)"
            )

        # File-order indices (no filename id matching)
        xq = np.arange(q.size)
        xm = np.arange(m.size)

        tag = "l" if which == "l" else "c"
        runs_path = self.out_dir / f"avg_{tag}_queue_delay_per_run.svg"
        hist_path = self.out_dir / f"avg_{tag}_queue_delay_triple_hist.svg"

        # ---------------------------------------------------------
        # Per-run connected plot (line plot instead of scatter)
        # ---------------------------------------------------------
        fig_runs = plt.figure(figsize=(10, 4))

        plt.plot(xq, q, marker="o", linestyle="-", label="qdisc (linux)")
        plt.plot(xm, m, marker="o", linestyle="-", label="mahimahi")

        plt.xlabel("run index (file order)")
        plt.ylabel("avg queue delay (ms)")
        plt.title(f"AVG_{tag.upper()}_QUEUE_DELAY — avg queue delay per run")

        plt.ticklabel_format(style="plain", axis="y")
        plt.grid(True, alpha=0.2)
        plt.legend()
        plt.tight_layout()

        fig_runs.savefig(runs_path, dpi=200, bbox_inches="tight")
        plt.close(fig_runs)

        # ---------------------------------------------------------
        # Triple histogram over |Δ|
        # ---------------------------------------------------------
        q_within = self._pairwise_abs_diffs(q)
        m_within = self._pairwise_abs_diffs(m)
        cross = self._cross_abs_diffs(q, m)

        fig_hist = self._plot_triple_hist_scalar_ms(
            q_within, m_within, cross, quantile=quantile
        )
        fig_hist.savefig(hist_path, dpi=200, bbox_inches="tight")
        plt.close(fig_hist)

        print(f"[AVG_{tag.upper()}_QUEUE_DELAY] qdisc N={q.size}  mahi N={m.size}")
        print(f"Saved: {runs_path.resolve()}")
        print(f"Saved: {hist_path.resolve()}")

    # =============================================================
    # Pipeline / DTW
    # =============================================================

    def run_parser(self):
        print(f"\n=== Running DTW Parser (variable = {self.dtw_mode}) ===")

        an = self._analyzer()
        an.compute_cross()
        an.compute_qdisc_internal()
        an.compute_mahi_internal()

        print("Saving cache...")
        an.save_cache()

        print("DTW Parsing Complete.")

    # =============================================================
    # Visualization helpers
    # =============================================================

    def view_histograms(self):
        # Special scalar modes (no DTW)
        if self.dtw_mode == "iperf_totalreceived":
            out_path = self.out_dir / "iperf_avg_bitrate_triple_hist.svg"
            run_iperf_totalreceived_histogram(
                qdisc_root=self.qdisc_dir,
                mahi_root=self.mahimahi_dir,
                out_path=out_path,
            )
            return
        if self.dtw_mode == "l_average_queue_delay":
            self.run_avg_queue_delay_histogram("l")
            return

        if self.dtw_mode == "c_average_queue_delay":
            self.run_avg_queue_delay_histogram("c")
            return

        # Default: DTW-based histogram
        an = self._analyzer()
        an.load_cache()
        fig = an.plot_triple_hist()
        self._save_fig(fig, f"triple_hist_{self.dtw_mode}.svg")

    def view_cdfs(self):
        # keep DTW-only for now (you didn’t ask for scalar CDFs)
        an = self._analyzer()
        an.load_cache()
        fig = an.plot_triple_cdf()
        self._save_fig(fig, f"triple_cdf_{self.dtw_mode}.svg")

    def plot_graph_check(self):
        an = self._analyzer()

        print("QDISC_DIR resolved to:", Path(self.qdisc_dir).resolve())
        print("Matched qdisc files:")
        for p in sorted(Path(self.qdisc_dir).glob("qdisc_*")):
            print("  ", p.name)

        fig = an.plot_overlay_queue_traces(cutoff_ms=0)
        self._save_fig(fig, f"overlay_{self.dtw_mode}.svg")

    # =============================================================
    # Nonparametric permutation test (DTW-only)
    # =============================================================

    def run_nonparametric_test(self):
        print(f"\n=== Running NONPARAMETRIC Permutation Test (variable = {self.dtw_mode}) ===")

        an = self._analyzer()
        an.load_cache()

        mahi_keys = list(an.mahi_dist.keys())
        qdisc_keys = list(an.qdisc_dist.keys())
        full = self._build_full_dist(an)

        self.test_obj = NonparametricDTWTest(full, mahi_keys, qdisc_keys)

        num = input("How many random permutations? [default = 20,000]: ").strip()
        num = int(num) if num else 20000

        print("\n--- Equivalence Threshold Setup ---")
        print("epsilon = acceptable DTW mean difference considered negligible.")
        eps_in = input("Enter epsilon: ").strip()
        try:
            epsilon = float(eps_in)
        except Exception:
            epsilon = 0.0

        self.test_results = self.test_obj.run(
            num_shuffles=num,
            epsilon=epsilon,
        )

        print("\n=== Permutation Test Finished ===")
        print(f"Observed M–Q Mean DTW = {self.test_results['original_mean']:.6f}")
        print(f"Num random ≥ observed = {self.test_results['num_ge']}/{num}")
        print(f"p-value = {self.test_results['p_value']:.6f}\n")

    def view_nonparametric_results(self):
        if self.test_results is None:
            print("Run the nonparametric test first.")
            return

        r = self.test_results
        print("\n=== Nonparametric Permutation Test Results ===")
        print(f"Observed Mean: {r['original_mean']:.6f}")

        num_ge = r.get("num_ge", r.get("num_greater"))
        total = r.get("num_shuffles_used", r.get("num_shuffles_requested"))
        print(f"Num shuffled ≥ observed: {num_ge}/{total}")
        print(f"p-value: {r['p_value']:.6f}")

        if r["p_value"] < 0.05:
            print("→ Evidence of DIFFERENCE (observed DTW unusually large under shuffling).")
        else:
            print("→ No strong evidence of difference.")

    def view_nonparametric_graph(self):
        if self.test_obj is None or self.test_results is None:
            print("Run the nonparametric test first.")
            return

        self.test_obj.plot(
            show=False,
            save_path=str(self.out_dir / f"perm_{self.dtw_mode}.svg"),
        )

    def plot_perm_convergence(self):
        print(f"\n=== Plot p-value vs permutations (variable = {self.dtw_mode}) ===")

        an = self._analyzer()
        an.load_cache()

        mahi_keys = list(an.mahi_dist.keys())
        qdisc_keys = list(an.qdisc_dist.keys())
        full = self._build_full_dist(an)

        self.test_obj = NonparametricDTWTest(full, mahi_keys, qdisc_keys)

        eps_in = input("Enter epsilon: ").strip()
        try:
            epsilon = float(eps_in)
        except Exception:
            epsilon = 0.0

        perm_list = np.unique(np.round(np.logspace(2, 5.3, 20)).astype(int)).tolist()

        self.test_obj.plot_pvalue_vs_perms(
            perm_list=perm_list,
            epsilon=epsilon,
            save_path=str(self.out_dir / f"pvalue_vs_perms_{self.dtw_mode}.svg"),
            show=False,
        )

    # =============================================================
    # Mode switches (callable)
    # =============================================================

    def set_mode(self, mode: str):
        self.dtw_mode = mode
        print(f"Switched variable → {mode}")

    def set_mode_ldelay(self):
        self.set_mode("qdelay_l_ms")
        print("Switched variable → L_QUEUE_DELAY (qdelay_l_ms)")

    def set_mode_cdelay(self):
        self.set_mode("qdelay_c_ms")
        print("Switched variable → C_QUEUE_DELAY (qdelay_c_ms)")

    def set_mode_l_average_queue_delay(self):
        self.set_mode("l_average_queue_delay")
        print("Switched variable → L_AVERAGE_QUEUE_DELAY (avg over delay_l / qdelay_l_ms)")

    def set_mode_c_average_queue_delay(self):
        self.set_mode("c_average_queue_delay")
        print("Switched variable → C_AVERAGE_QUEUE_DELAY (avg over delay_c / qdelay_c_ms)")

    def set_mode_iperf_totalreceived(self):
        self.set_mode("iperf_totalreceived")
        print("Switched variable → IPERF_TOTALRECEIVED (from iperf3 *server* logs)")

    def set_mode_packets(self):
        self.set_mode("packets")
        print("Switched variable → PACKETS (q_pkts)")

    def set_mode_bytes(self):
        self.set_mode("bytes")
        print("Switched variable → BYTES (q_bytes)")

    def set_mode_ecn(self):
        self.set_mode("ecn_mark")
        print("Switched variable → ECN_MARK (ecn_mark)")

    def set_mode_time(self):
        self.set_mode("t_ms")
        print("Switched variable → TIME (t_ms)")

    def set_mode_packet_dropped_total(self):
        self.set_mode("packet_dropped_total")
        print("Switched variable → PACKET_DROPPED_TOTAL")

    def set_mode_packet_dropped_l4s(self):
        self.set_mode("packet_dropped_l4s")
        print("Switched variable → PACKET_DROPPED_L4S")

    def set_mode_packet_dropped_classic(self):
        self.set_mode("packet_dropped_classic")
        print("Switched variable → PACKET_DROPPED_CLASSIC")

    # =============================================================
    # Menu
    # =============================================================

    def menu(self):
        while True:
            print("\n===============================")
            print("   Validation Tool for AQM statuses")
            print("===============================")
            print(f"Current variable: {self.dtw_mode.upper()}")
            print("")
            print("1. Run Parser (compute DTW + save cache)")
            print("2. View Histograms")
            print("3. View CDFs")
            print("4. Plot Graph Check (Mahimahi vs Kernel overlay)")
            print("5. Run NONPARAMETRIC Permutation Test")
            print("6. View Permutation Test Results")
            print("7. View Permutation Test Graph")
            print("15. Plot p-value vs permutations")
            print("------ VARIABLE SWITCH ------")
            print("8.  Use PACKETS (q_pkts)")
            print("9.  Use BYTES (q_bytes)")
            print("10. Use ECN_MARK (ecn_mark)")
            print("11. Use TIME (t_ms)")
            print("12. Use PACKET_DROPPED_TOTAL")
            print("13. Use PACKET_DROPPED_L4S")
            print("14. Use PACKET_DROPPED_CLASSIC")
            print("16. Use IPERF_TOTALRECEIVED (iperf3 server logs)")
            print("17. Use L_QUEUE_DELAY (qdelay_l_ms)")
            print("18. Use C_QUEUE_DELAY (qdelay_c_ms)")
            print("19. Use L_AVERAGE_QUEUE_DELAY (no DTW; avg per run)")
            print("20. Use C_AVERAGE_QUEUE_DELAY (no DTW; avg per run)")
            print("")
            print("0. Exit")
            print("===============================")

            choice = input("Enter choice: ").strip()

            if choice == "1":
                self.run_parser()
            elif choice == "2":
                self.view_histograms()
            elif choice == "3":
                self.view_cdfs()
            elif choice == "4":
                self.plot_graph_check()
            elif choice == "5":
                self.run_nonparametric_test()
            elif choice == "6":
                self.view_nonparametric_results()
            elif choice == "7":
                self.view_nonparametric_graph()
            elif choice == "8":
                self.set_mode_packets()
            elif choice == "9":
                self.set_mode_bytes()
            elif choice == "10":
                self.set_mode_ecn()
            elif choice == "11":
                self.set_mode_time()
            elif choice == "12":
                self.set_mode_packet_dropped_total()
            elif choice == "13":
                self.set_mode_packet_dropped_l4s()
            elif choice == "14":
                self.set_mode_packet_dropped_classic()
            elif choice == "15":
                self.plot_perm_convergence()
            elif choice == "16":
                self.set_mode_iperf_totalreceived()
            elif choice == "17":
                self.set_mode_ldelay()
            elif choice == "18":
                self.set_mode_cdelay()
            elif choice == "19":
                self.set_mode_l_average_queue_delay()
            elif choice == "20":
                self.set_mode_c_average_queue_delay()
            elif choice == "0":
                print("Exiting.")
                return
            else:
                print("Invalid choice.")


if __name__ == "__main__":
    tool = AQMValidationTool(
        qdisc_dir="../qdisc_logs",
        mahimahi_dir="../mahimahi_logs",
        out_dir="./figs",
        dtw_mode="packets",
    )
    tool.menu()