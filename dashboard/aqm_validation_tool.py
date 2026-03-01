from __future__ import annotations

from pathlib import Path
import re
import numpy as np

# UPDATED: new analyzer location (parser.py got split)
from mahimahi_validation.dashboard.validation_steps.dtw_analyzer import DTWAnalyzer

from mahimahi_validation.dashboard.validation_steps.permutation_test import (
    NonparametricDTWTest,
)

# UPDATED: you renamed validation.py -> timereseires_validation.py
from mahimahi_validation.dashboard.validation_steps.timeseries_validation import (
    plot_validation_hist,
    bootstrap_ci_pmax,
    plot_bootstrap_ci_vs_n,
    print_bootstrap_ci_vs_n,
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mahimahi_validation.dashboard.validation_steps.avg_bitrate_validation import AvgBitrateValidation

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
    _DELAY_RE = re.compile(
        r"\bdelay_c\s+(\d+)([a-zA-Z]+)\s+delay_l\s+(\d+)([a-zA-Z]+)\b"
    )

    def __init__(
        self,
        qdisc_dir: str | Path = "../qdisc_logs",
        mahimahi_dir: str | Path = "../mahimahi_logs",
        out_dir: str | Path = "./figs",
        dtw_mode: str = "packets",
        bandwidth: str = "200Mbps",
    ):
        self.qdisc_dir = str(qdisc_dir)
        self.mahimahi_dir = str(mahimahi_dir)

        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # dtw_mode:
        #   - DTW modes: "packets", "bytes", "ecn_mark", "t_ms", "qdelay_l_ms", "qdelay_c_ms", "packet_dropped_*"
        #   - scalar modes: "iperf_totalreceived", "l_average_queue_delay", "c_average_queue_delay"
        self.dtw_mode = dtw_mode
        self.bandwidth = bandwidth

        self.test_obj: NonparametricDTWTest | None = None
        self.test_results: dict | None = None

    # =============================================================
    # Internals
    # =============================================================
    def _include_validation_xaxis_label(self) -> bool:
        return self.bandwidth == "12Mbps"

    def _save_fig(self, fig, name: str):
        path = self.out_dir / name
        fig.savefig(path, dpi=200)  # no bbox_inches="tight"
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
    # DTW-only plot helpers (moved out of old parser.py)
    # =============================================================
    @staticmethod
    def _extract_values(pairs):
        return [d for (_, _, d) in pairs]

    def _plot_triple_hist_dtw(self, an: DTWAnalyzer):
        qvals = np.asarray(self._extract_values(an.qdisc_internal), dtype=float)
        mvals = np.asarray(self._extract_values(an.mahi_internal), dtype=float)
        cvals = np.asarray(self._extract_values(an.cross_results), dtype=float)

        qfinite = qvals[np.isfinite(qvals)]
        mfinite = mvals[np.isfinite(mvals)]
        cfinite = cvals[np.isfinite(cvals)]

        q95 = float(np.percentile(qfinite, 95)) if qfinite.size else np.nan
        m95 = float(np.percentile(mfinite, 95)) if mfinite.size else np.nan

        eps_min = min(q95, m95)
        eps_max = max(q95, m95)

        n = int(cfinite.size)
        if n == 0 or not np.isfinite(eps_min) or not np.isfinite(eps_max):
            p_min = np.nan
            p_max = np.nan
        else:
            p_min = float((np.sum(cfinite > eps_min) + 1) / (n + 1))
            p_max = float((np.sum(cfinite > eps_max) + 1) / (n + 1))

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        axes[0].hist(qfinite, bins=30, edgecolor="black")
        axes[0].axvline(q95, linewidth=1.5, linestyle="--")
        axes[0].set_title(f"Qdisc DTW Histogram ({an.mode})\nq95={q95:.4g}")

        axes[1].hist(mfinite, bins=30, edgecolor="black")
        axes[1].axvline(m95, linewidth=1.5, linestyle="--")
        axes[1].set_title(f"Mahimahi DTW Histogram ({an.mode})\nm95={m95:.4g}")

        axes[2].hist(cfinite, bins=30, edgecolor="black")
        axes[2].axvline(eps_min, linewidth=1.5, linestyle="--")
        axes[2].axvline(eps_max, linewidth=1.5, linestyle="--")
        axes[2].set_title(
            f"Cross-Mode DTW Histogram ({an.mode})\n"
            f"eps_min={eps_min:.4g}, p_min={p_min:.4g} | "
            f"eps_max={eps_max:.4g}, p_max={p_max:.4g}"
        )

        fig.tight_layout()
        return fig

    def _plot_triple_cdf_dtw(self, an: DTWAnalyzer):
        qvals = sorted(self._extract_values(an.qdisc_internal))
        mvals = sorted(self._extract_values(an.mahi_internal))
        cvals = sorted(self._extract_values(an.cross_results))

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        if qvals:
            axes[0].plot(qvals, [i / len(qvals) for i in range(len(qvals))])
        axes[0].set_title(f"Qdisc DTW CDF ({an.mode})")

        if mvals:
            axes[1].plot(mvals, [i / len(mvals) for i in range(len(mvals))])
        axes[1].set_title(f"Mahimahi DTW CDF ({an.mode})")

        if cvals:
            axes[2].plot(cvals, [i / len(cvals) for i in range(len(cvals))])
        axes[2].set_title(f"Cross-Mode DTW CDF ({an.mode})")

        plt.tight_layout()
        return fig

    @staticmethod
    def _robust_ylim(all_y, lo=1, hi=99, pad_frac=0.05):
        ys = []
        for y in all_y:
            if y:
                ys.extend(y)
        if not ys:
            return None

        ys = np.asarray(ys, dtype=float)
        ys = ys[np.isfinite(ys)]
        if ys.size == 0:
            return None

        y0 = float(np.percentile(ys, lo))
        y1 = float(np.percentile(ys, hi))

        if y0 == y1:
            y0 = float(np.min(ys))
            y1 = float(np.max(ys))
            if y0 == y1:
                y0 -= 1.0
                y1 += 1.0

        pad = (y1 - y0) * pad_frac
        return (y0 - pad, y1 + pad)

    def _plot_overlay_queue_traces(self, an: DTWAnalyzer, cutoff_ms: int = 0):
        # glob patterns match your tool’s assumptions
        mahi_files = sorted(Path(self.mahimahi_dir).glob("output_*"))
        qdisc_files = sorted(Path(self.qdisc_dir).glob("qdisc_*"))

        fig, axes = plt.subplots(1, 2, figsize=(18, 5))

        all_y_plotted = []
        global_xmax = 0.0

        ax = axes[0]
        for f in mahi_files:
            tr = an.read_mahi_trace_full(f)
            t_ms, y = an.time_and_series_for_compare(tr)
            if not t_ms or not y:
                continue

            k = 0
            while k < len(t_ms) and t_ms[k] < cutoff_ms:
                k += 1
            t2 = t_ms[k:]
            y2 = y[k:]
            if not y2:
                continue

            ax.plot(t2, y2, linewidth=0.6, marker="o", markersize=2)
            all_y_plotted.append(y2)
            global_xmax = max(global_xmax, float(t2[-1]))

        ax.set_title(f"Mahimahi {an.mode} vs time (overlay)")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(an.mode)
        ax.grid(True, alpha=0.2)

        ax = axes[1]
        for f in qdisc_files:
            tr = an.read_qdisc_trace_full(f)
            t_ms, y = an.time_and_series_for_compare(tr)
            if not t_ms or not y:
                continue

            k = 0
            while k < len(t_ms) and t_ms[k] < cutoff_ms:
                k += 1
            t2 = t_ms[k:]
            y2 = y[k:]
            if not y2:
                continue

            ax.plot(t2, y2, linewidth=0.6, marker="o", markersize=2)
            all_y_plotted.append(y2)
            global_xmax = max(global_xmax, float(t2[-1]))

        ax.set_title(f"Linux qdisc {an.mode} vs time (overlay)")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(an.mode)
        ax.grid(True, alpha=0.2)

        ylim = self._robust_ylim(all_y_plotted, lo=1, hi=99, pad_frac=0.05)
        if ylim is not None:
            for ax in axes:
                ax.set_ylim(*ylim)
                ax.set_xlim(0, global_xmax)

        plt.tight_layout()
        return fig

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
        return v / 1000.0

    def _collect_id_files(
        self, root: str | Path, *, glob_pattern: str = "*"
    ) -> list[tuple[int, Path]]:
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
        key = "qdelay_l_ms=" if which == "l" else "qdelay_c_ms="
        vals = []
        try:
            for line in path.read_text(errors="ignore").splitlines():
                if not line.startswith("[QUEUE_STATS]"):
                    continue
                for tok in line.split():
                    if tok.startswith(key):
                        v = tok[len(key) :].strip().rstrip(",")
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
                            c_val = int(m.group(1))
                            c_unit = m.group(2)
                            l_val = int(m.group(3))
                            l_unit = m.group(4)
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

    def _plot_triple_hist_scalar_ms(
        self,
        q_within: np.ndarray,
        m_within: np.ndarray,
        cross: np.ndarray,
        *,
        quantile: float = 95.0,
    ):
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

    def run_avg_queue_delay_histogram(
        self,
        which: str,
        *,
        quantile: float = 95.0,
        val_path: Path | None = None,
    ):
        q_items = self._collect_id_files(self.qdisc_dir, glob_pattern="qdisc_*")
        m_items = self._collect_id_files(self.mahimahi_dir, glob_pattern="output_*")

        q = np.asarray(
            [self._avg_delay_ms_from_qdisc_file(p, which) for _, p in q_items],
            dtype=np.float64,
        )
        m = np.asarray(
            [self._avg_delay_ms_from_mahi_file(p, which) for _, p in m_items],
            dtype=np.float64,
        )

        if q.size == 0 or m.size == 0:
            raise RuntimeError(f"Need non-empty samples. qdisc={q.size}, mahi={m.size}")

        xq = np.arange(q.size)
        xm = np.arange(m.size)

        tag = "l" if which == "l" else "c"
        runs_path = self.out_dir / f"avg_{tag}_queue_delay_per_run.svg"
        hist_path = self.out_dir / f"avg_{tag}_queue_delay_triple_hist.svg"

        # Per-run connected plot
        fig_runs = plt.figure(figsize=(10, 4))
        ax_runs = plt.gca()

        ax_runs.plot(xq, q, marker="o", linestyle="-", label="qdisc (linux)")
        ax_runs.plot(xm, m, marker="o", linestyle="-", label="mahimahi")

        ax_runs.set_xlabel("run index (file order)", fontsize=14)
        ax_runs.set_ylabel("avg queue delay (ms)", fontsize=14)
        ax_runs.set_title(f"AVG_{tag.upper()}_QUEUE_DELAY — avg queue delay per run", fontsize=16)

        ax_runs.tick_params(axis="both", labelsize=12)
        plt.setp(ax_runs.get_xticklabels(), rotation=30, ha="right")

        ax_runs.ticklabel_format(style="plain", axis="y")
        ax_runs.grid(True, alpha=0.2)
        ax_runs.legend(fontsize=12)

        fig_runs.tight_layout()
        fig_runs.savefig(runs_path, dpi=200)
        plt.close(fig_runs)

        # Triple histogram
        q_within = self._pairwise_abs_diffs(q)
        m_within = self._pairwise_abs_diffs(m)
        cross = self._cross_abs_diffs(q, m)

        fig_hist = self._plot_triple_hist_scalar_ms(q_within, m_within, cross, quantile=quantile)
        fig_hist.savefig(hist_path, dpi=200)
        plt.close(fig_hist)

        # Validation-only histogram (scalar) — keep your existing style
        if cross.size == 0:
            raise RuntimeError("Validation histogram needs non-empty cross diffs.")
        if val_path is None:
            raise RuntimeError("val_path must be provided to save validation histogram.")

        q_thr = float(np.percentile(q_within, quantile)) if q_within.size else np.nan
        m_thr = float(np.percentile(m_within, quantile)) if m_within.size else np.nan
        eps_min = min(q_thr, m_thr)
        eps_max = max(q_thr, m_thr)

        n = int(cross.size)
        if n == 0 or not np.isfinite(eps_min) or not np.isfinite(eps_max):
            p_min = np.nan
            p_max = np.nan
        else:
            p_min = float((np.sum(cross > eps_min) + 1) / (n + 1))
            p_max = float((np.sum(cross > eps_max) + 1) / (n + 1))

        cfinite = cross[np.isfinite(cross)]
        if cfinite.size == 0:
            raise RuntimeError("No finite cross values for validation histogram.")

        fig_val = plt.figure(figsize=(8.5, 5))
        ax = plt.gca()

        for spine in ax.spines.values():
            spine.set_linewidth(0.8)

        ax.hist(
            cfinite,
            bins=30,
            color="#87CEEB",
            edgecolor="black",
            alpha=0.85,
        )

        if np.isfinite(eps_min):
            ax.axvline(eps_min, linestyle="--", linewidth=1.5)
        if np.isfinite(eps_max):
            ax.axvline(eps_max, linestyle="--", linewidth=1.5)

        ax.set_xlabel("|qdisc - mahi| (ms)")
        ax.set_ylabel("frequency")
        ax.xaxis.label.set_visible(self.bandwidth == "12Mbps")
        ax.yaxis.label.set_visible(True)

        ax.set_title("")
        ax.xaxis.label.set_fontsize(16)
        ax.yaxis.label.set_fontsize(16)
        ax.tick_params(axis="both", labelsize=16)

        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        plt.setp(ax.get_yticklabels(), rotation=30, va="center")

        ax.grid(True, alpha=0.2)

        header = (
            f"eps_min={eps_min:.4g}, p_min={p_min:.4g}   |   "
            f"eps_max={eps_max:.4g}, p_max={p_max:.4g}"
        )
        ax.text(
            0.5,
            1.04,
            header,
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=16,
        )

        fig_val.tight_layout()
        fig_val.savefig(val_path, dpi=200)
        plt.close(fig_val)

        print(f"[AVG_{tag.upper()}_QUEUE_DELAY] qdisc N={q.size}  mahi N={m.size}")
        print(f"Saved: {runs_path.resolve()}")
        print(f"Saved: {hist_path.resolve()}")
        print(f"Saved: {Path(val_path).resolve()}")

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
    # NEW: CI helpers (DTW-only)
    # =============================================================
    def print_ci(self, *, quantile=95.0, B=2000, alpha=0.05, seed=0):
        """
        Prints bootstrap CI for p_max (run-ID resampling). DTW-only.
        """
        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

        p_lo, p_hi, p_upper_1s = bootstrap_ci_pmax(
            an, quantile=quantile, B=B, alpha=alpha, seed=seed
        )
        print(
            f"[CI] {int((1-alpha)*100)}% CI for p_max: [{p_lo:.4f}, {p_hi:.4f}] | "
            f"upper 1-sided ({int((1-alpha)*100)}%): {p_upper_1s:.4f}"
        )

    def plot_ci_width_vs_n(
        self,
        *,
        ns=(10, 25, 40, 60, 80, 90, 95, 100),
        quantile=95.0,
        B=2000,
        alpha=0.05,
        seed=0,
        show_yaxis=True,   # NEW
    ):
        """
        Saves CI width vs n into figs/, auto-suffix if file exists.
        DTW-only.
        """
        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

        base_name = f"{self.dtw_mode}_ci_width_vs_n"
        out_path = self.out_dir / f"{base_name}.svg"
        k = 2
        while out_path.exists():
            out_path = self.out_dir / f"{base_name}_{k}.svg"
            k += 1

        plot_bootstrap_ci_vs_n(
            an,
            ns=ns,
            quantile=quantile,
            B=B,
            alpha=alpha,
            seed=seed,
            out_path=out_path,
            show_yaxis=show_yaxis,   # PASS THROUGH
        )

        print(f"Saved: {out_path.resolve()}")

    def print_ci_vs_n(
        self,
        *,
        ns=(10, 25, 40, 60, 80, 100),
        quantile=95.0,
        B=2000,
        alpha=0.05,
        seed=0,
    ):
        """
        Prints bootstrap CI vs n (DTW-only).
        """
        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

        print_bootstrap_ci_vs_n(
            an,
            ns=ns,
            quantile=quantile,
            B=B,
            alpha=alpha,
            seed=seed,
        )

    # =============================================================
    # Visualization helpers
    # =============================================================
    def run_iperf_totalreceived_validation_hist(self):
        filename = f"paper-graphs-{self.bandwidth}-reject_hist_{self.dtw_mode}.svg"
        out_path = self.out_dir / filename

        AvgBitrateValidation().run_avg_bitrate_histogram(
            qdisc_root=self.qdisc_dir,
            mahi_root=self.mahimahi_dir,
            out_path=out_path,
            quantile=95.0,
            include_x_axis_label=self._include_validation_xaxis_label(),
            include_y_axis_label=True,
        )

    def view_histograms(self):
        filename = f"paper-graphs-{self.bandwidth}-reject_hist_{self.dtw_mode}.svg"

        # Scalar modes (no DTW)
        if self.dtw_mode == "iperf_totalreceived":
            self.run_iperf_totalreceived_validation_hist()
            return
        if self.dtw_mode == "l_average_queue_delay":
            self.run_avg_queue_delay_histogram("l", val_path=self.out_dir / filename)
            return
        if self.dtw_mode == "c_average_queue_delay":
            self.run_avg_queue_delay_histogram("c", val_path=self.out_dir / filename)
            return

        # DTW-based
        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

        fig_triple = self._plot_triple_hist_dtw(an)
        self._save_fig(fig_triple, f"triple_hist_{self.dtw_mode}.svg")
        plt.close(fig_triple)

        # OPTIONAL: compute CI and pass into renderer
        # p_lo, p_hi, p_upper_1s = bootstrap_ci_pmax(an, quantile=95.0, B=2000, alpha=0.05, seed=0)

        fig_val = plot_validation_hist(
            an,
            include_x_axis_label=self._include_validation_xaxis_label(),
            include_y_axis_label=(self.dtw_mode == "iperf_totalreceived"),
            # p_ci_lo=p_lo,
            # p_ci_hi=p_hi,
            # p_upper_1s=p_upper_1s,
        )
        self._save_fig(fig_val, filename)
        plt.close(fig_val)

    def view_cdfs(self):
        if self.dtw_mode in ("iperf_totalreceived", "l_average_queue_delay", "c_average_queue_delay"):
            print("CDFs currently DTW-only (not scalar).")
            return

        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

        fig = self._plot_triple_cdf_dtw(an)
        self._save_fig(fig, f"triple_cdf_{self.dtw_mode}.svg")
        plt.close(fig)

    def plot_graph_check(self):
        if self.dtw_mode in ("iperf_totalreceived", "l_average_queue_delay", "c_average_queue_delay"):
            print("Overlay is DTW time-series only (not scalar summary modes).")
            return

        an = self._analyzer()

        print("QDISC_DIR resolved to:", Path(self.qdisc_dir).resolve())
        print("Matched qdisc files:")
        for p in sorted(Path(self.qdisc_dir).glob("qdisc_*")):
            print("  ", p.name)

        fig = self._plot_overlay_queue_traces(an, cutoff_ms=0)
        self._save_fig(fig, f"overlay_{self.dtw_mode}.svg")
        plt.close(fig)

    # =============================================================
    # Nonparametric permutation test (DTW-only)
    # =============================================================
    def run_nonparametric_test(self):
        if self.dtw_mode in ("iperf_totalreceived", "l_average_queue_delay", "c_average_queue_delay"):
            print("Permutation test currently DTW-only (not scalar).")
            return

        print(f"\n=== Running NONPARAMETRIC Permutation Test (variable = {self.dtw_mode}) ===")

        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

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
        if self.dtw_mode in ("iperf_totalreceived", "l_average_queue_delay", "c_average_queue_delay"):
            print("Permutation convergence plot currently DTW-only (not scalar).")
            return

        print(f"\n=== Plot p-value vs permutations (variable = {self.dtw_mode}) ===")

        an = self._analyzer()
        if not an.load_cache():
            raise RuntimeError("Cache not found. Run parser first (option 1).")

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

    def set_mode_packets(self):
        self.set_mode("packets")

    def set_mode_bytes(self):
        self.set_mode("bytes")

    def set_mode_ecn(self):
        self.set_mode("ecn_mark")

    def set_mode_time(self):
        self.set_mode("t_ms")

    def set_mode_packet_dropped_total(self):
        self.set_mode("packet_dropped_total")

    def set_mode_packet_dropped_l4s(self):
        self.set_mode("packet_dropped_l4s")

    def set_mode_packet_dropped_classic(self):
        self.set_mode("packet_dropped_classic")

    def set_mode_iperf_totalreceived(self):
        self.set_mode("iperf_totalreceived")

    def set_mode_ldelay(self):
        self.set_mode("qdelay_l_ms")

    def set_mode_cdelay(self):
        self.set_mode("qdelay_c_ms")

    def set_mode_l_average_queue_delay(self):
        self.set_mode("l_average_queue_delay")

    def set_mode_c_average_queue_delay(self):
        self.set_mode("c_average_queue_delay")

    # =============================================================
    # Menu (clean numbering)
    # =============================================================
    def menu(self):
        while True:
            print("\n===============================")
            print("   Validation Tool for AQM statuses")
            print("===============================")
            print(f"Current variable: {self.dtw_mode}")
            print("")
            print("1.  Run Parser (compute DTW + save cache)")
            print("2.  View Histograms (triple + validation)")
            print("3.  View CDFs (DTW-only)")
            print("4.  Plot Graph Check (overlay traces; DTW-only)")
            print("5.  Run NONPARAMETRIC Permutation Test (DTW-only)")
            print("6.  View Permutation Test Results")
            print("7.  View Permutation Test Graph")
            print("8.  Plot p-value vs permutations (DTW-only)")
            print("9.  Print bootstrap CI for p_max (DTW-only)")
            print("10. Print bootstrap CI vs n (DTW-only)")
            print("11. Plot CI width vs n (DTW-only)")
            print("------ VARIABLE SWITCH ------")
            print("12. PACKETS (q_pkts)")
            print("13. BYTES (q_bytes)")
            print("14. ECN_MARK (ecn_mark)")
            print("15. TIME (t_ms)")
            print("16. PACKET_DROPPED_TOTAL")
            print("17. PACKET_DROPPED_L4S")
            print("18. PACKET_DROPPED_CLASSIC")
            print("19. IPERF_TOTALRECEIVED (iperf3 server logs) [scalar]")
            print("20. L_QUEUE_DELAY (qdelay_l_ms)")
            print("21. C_QUEUE_DELAY (qdelay_c_ms)")
            print("22. L_AVERAGE_QUEUE_DELAY (no DTW; avg per run) [scalar]")
            print("23. C_AVERAGE_QUEUE_DELAY (no DTW; avg per run) [scalar]")
            print("")
            print("0.  Exit")
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
                self.plot_perm_convergence()
            elif choice == "9":
                self.print_ci()
            elif choice == "10":
                self.print_ci_vs_n()
            elif choice == "11":
                self.plot_ci_width_vs_n()
            elif choice == "12":
                self.set_mode_packets()
            elif choice == "13":
                self.set_mode_bytes()
            elif choice == "14":
                self.set_mode_ecn()
            elif choice == "15":
                self.set_mode_time()
            elif choice == "16":
                self.set_mode_packet_dropped_total()
            elif choice == "17":
                self.set_mode_packet_dropped_l4s()
            elif choice == "18":
                self.set_mode_packet_dropped_classic()
            elif choice == "19":
                self.set_mode_iperf_totalreceived()
            elif choice == "20":
                self.set_mode_ldelay()
            elif choice == "21":
                self.set_mode_cdelay()
            elif choice == "22":
                self.set_mode_l_average_queue_delay()
            elif choice == "23":
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
        bandwidth="200Mbps",
    )
    tool.menu()