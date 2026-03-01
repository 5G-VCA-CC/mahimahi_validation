# validation_steps/min.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.ticker import ScalarFormatter

@dataclass
class AvgBitrateValidation:
    """
    Validate avg throughput similarity by comparing within-system |Δ| vs cross-system |Δ|.

    Workflow:
      1) Parse per-run avg bitrate from iperf3 server logs (classic + l4s summed)
      2) Compute within-system pairwise |Δ| distributions for qdisc and mahimahi
      3) Set epsilon thresholds from within-system quantiles
      4) Compute cross-system exceedance probabilities p_min / p_max
      5) (Optional) bootstrap CI over runs (resample run-level averages)
      6) Render histogram of cross |Δ| with epsilon line and title p_hat_max
    """

    # iperf3 parsing
    summary_re: re.Pattern = re.compile(
        r"^\[\s*\d+\]\s+"
        r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s+sec\s+"
        r"(\d+(?:\.\d+)?)\s+([KMGTP]?Bytes)\s+"
        r"(\d+(?:\.\d+)?)\s+([KMGTP]?bits/sec)\s+"
        r".*\breceiver\b"
    )
    unit_bits: dict[str, float] = None  # set in __post_init__
    id_re: re.Pattern = re.compile(r"^iperf3_(\d+)\.(classic|l4s)\.server\.log$")

    def __post_init__(self):
        if self.unit_bits is None:
            self.unit_bits = {
                "bits/sec": 1.0,
                "Kbits/sec": 1e3,
                "Mbits/sec": 1e6,
                "Gbits/sec": 1e9,
                "Tbits/sec": 1e12,
                "Pbits/sec": 1e15,
            }

    # ============================================================
    # iperf3 server summary parsing
    # ============================================================
    def _parse_avg_bitrate_bps_from_server_log(self, path: Path) -> float:
        try:
            txt = path.read_text(errors="ignore")
        except FileNotFoundError:
            return 0.0

        last = None
        for line in txt.splitlines():
            m = self.summary_re.match(line.strip())
            if not m:
                continue
            br = float(m.group(5))
            unit = m.group(6)
            last = br * self.unit_bits.get(unit, 1.0)

        return 0.0 if last is None else float(last)

    # ============================================================
    # Discover runs (log discovery helpers)
    # ============================================================
    def _collect_run_ids(self, root: Path) -> list[int]:
        ids: set[int] = set()
        for p in Path(root).rglob("iperf3_*.server.log"):
            m = self.id_re.match(p.name)
            if m:
                ids.add(int(m.group(1)))
        return sorted(ids)

    def _find_one(self, root: Path, filename: str) -> Path | None:
        for p in Path(root).rglob(filename):
            return p
        return None

    def _total_bitrate_bps_for_id(self, root: Path, run_id: int) -> float:
        classic_name = f"iperf3_{run_id}.classic.server.log"
        l4s_name = f"iperf3_{run_id}.l4s.server.log"

        classic = self._find_one(root, classic_name)
        l4s = self._find_one(root, l4s_name)

        total = 0.0
        if classic is not None:
            total += self._parse_avg_bitrate_bps_from_server_log(classic)
        if l4s is not None:
            total += self._parse_avg_bitrate_bps_from_server_log(l4s)
        return float(total)

    def load_group_bitrates(self, root: str | Path) -> tuple[np.ndarray, np.ndarray]:
        root = Path(root)
        ids = self._collect_run_ids(root)
        vals = [self._total_bitrate_bps_for_id(root, rid) for rid in ids]
        return np.asarray(ids, dtype=np.int64), np.asarray(vals, dtype=np.float64)

    # ============================================================
    # |Δ| distributions (helpers)
    # ============================================================
    @staticmethod
    def pairwise_abs_diffs(x: np.ndarray) -> np.ndarray:
        n = x.size
        if n < 2:
            return np.asarray([], dtype=np.float64)
        vals: list[float] = []
        for i in range(n):
            for j in range(i + 1, n):
                vals.append(abs(float(x[i]) - float(x[j])))
        return np.asarray(vals, dtype=np.float64)

    @staticmethod
    def cross_abs_diffs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        if a.size == 0 or b.size == 0:
            return np.asarray([], dtype=np.float64)
        vals: list[float] = []
        for ai in a:
            fa = float(ai)
            for bj in b:
                vals.append(abs(fa - float(bj)))
        return np.asarray(vals, dtype=np.float64)

    # ============================================================
    # Bootstrap CI for validation metric (scalar throughput version)
    # ============================================================
    def bootstrap_validation_ci(
        self,
        q: np.ndarray,
        m: np.ndarray,
        *,
        quantile: float = 95.0,
        B: int = 2000,
        alpha: float = 0.05,
        seed: int = 0,
    ) -> tuple[float, tuple[float, float], float, float, tuple[float, float]]:
        """
        Bootstrap CI for:
          - p_max = P(cross_diff > eps_max)
          - eps_max itself

        Returns:
          (p_hat, (p_lo, p_hi), p_upper_1s, eps_hat, (eps_lo, eps_hi))
        """
        rng = np.random.default_rng(seed)

        def _compute(qs: np.ndarray, ms: np.ndarray) -> tuple[float, float]:
            q_within = self.pairwise_abs_diffs(qs)
            m_within = self.pairwise_abs_diffs(ms)
            cross = self.cross_abs_diffs(qs, ms)

            qfinite = q_within[np.isfinite(q_within)]
            mfinite = m_within[np.isfinite(m_within)]
            cfinite = cross[np.isfinite(cross)]

            q_thr = float(np.percentile(qfinite, quantile))
            m_thr = float(np.percentile(mfinite, quantile))
            eps_max = max(q_thr, m_thr)

            n = int(cfinite.size)
            p_max = float((np.sum(cfinite > eps_max) + 1) / (n + 1))
            return p_max, float(eps_max)

        p_hat, eps_hat = _compute(q, m)

        p_boot = np.empty(B, dtype=float)
        e_boot = np.empty(B, dtype=float)

        nq, nm = int(q.size), int(m.size)
        for b in range(B):
            qb = q[rng.integers(0, nq, size=nq)]
            mb = m[rng.integers(0, nm, size=nm)]
            p_boot[b], e_boot[b] = _compute(qb, mb)

        p_lo, p_hi = np.quantile(p_boot, [alpha / 2, 1 - alpha / 2])
        p_upper_1s = np.quantile(p_boot, 1 - alpha)

        e_lo, e_hi = np.quantile(e_boot, [alpha / 2, 1 - alpha / 2])

        return float(p_hat), (float(p_lo), float(p_hi)), float(p_upper_1s), float(eps_hat), (float(e_lo), float(e_hi))

    # ============================================================
    # Plotting helpers
    # ============================================================
    @staticmethod
    def _fmt_sci_tex(x: float, sig: int = 4) -> str:
        """Return LaTeX like 5×10^{-5} (or plain decimal if not tiny)."""
        if not np.isfinite(x):
            return r"\mathrm{nan}"
        if x == 0:
            return "0"
        ax = abs(x)
        if ax < 1e-3 or ax >= 1e4:
            s = f"{x:.{sig}e}"
            mant, exp = s.split("e")
            mant = mant.rstrip("0").rstrip(".")
            exp = int(exp)
            return rf"{mant}\times 10^{{{exp}}}"
        return f"{x:.{sig}g}"

    def _render_validation_histogram(
        self,
        values: np.ndarray,
        eps_max: float,
        p_max: float,
        *,
        x_label: str,
        include_x_axis_label: bool,
        include_y_axis_label: bool,
        out_path: str | Path | None = None,
    ):
        plt.rcParams.update(
            {
                "font.family": "DejaVu Sans",
                "mathtext.fontset": "dejavusans",
                "svg.fonttype": "path",
            }
        )

        fig, ax = plt.subplots(figsize=(9, 7))

        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_linewidth(0.8)

        v = np.asarray(values, dtype=float)
        v = v[np.isfinite(v)]
        if v.size == 0:
            v = np.array([0.0], dtype=float)

        vmax = float(np.max(v))
        q01 = float(np.percentile(v, 1))
        q99 = float(np.percentile(v, 99))
        spread = q99 - q01

        SPREAD_EPS = 1e-6
        ZERO_EPS = 1e-12
        XMAX_FALLBACK = 0.05

        is_all_zeroish = (float(np.max(np.abs(v))) <= ZERO_EPS)
        is_degenerate = is_all_zeroish or (spread < SPREAD_EPS)

        if is_degenerate:
            x_max = XMAX_FALLBACK
            if np.isfinite(eps_max) and float(eps_max) > 0:
                x_max = max(x_max, float(eps_max) * 1.10)
            if vmax > 0:
                x_max = max(x_max, vmax)

            ax.set_xlim(0.0, x_max)

            width = 0.02 * x_max
            width = float(np.clip(width, 0.002, 0.01))

            ax.bar(
                0.0,
                v.size,
                width=width,
                edgecolor="black",
                alpha=0.85,
                align="center",
            )
        else:
            ax.hist(
                v,
                bins=30,
                range=(0.0, vmax),
                edgecolor="black",
                alpha=0.85,
            )
            ax.set_xlim(0.0, vmax)

        ax.set_xlim(left=0.0)
        ax.autoscale(enable=False, axis="x")
        ax.autoscale(enable=False, axis="y")

        TITLE_SIZE = 40
        AXIS_LABEL_SIZE = 36
        TICK_SIZE = 28
        Y_TICK_SIZE = 28
        OFFSET_SIZE = 34   # 👈 scientific notation size

        xlab = x_label.replace("Avg", "Average").replace("avg", "average")
        ax.set_xlabel(xlab, fontsize=AXIS_LABEL_SIZE)
        ax.set_ylabel("Frequency", fontsize=AXIS_LABEL_SIZE)

        ax.minorticks_off()
        ax.xaxis.set_minor_locator(mticker.NullLocator())
        ax.xaxis.set_major_locator(mticker.MaxNLocator(nbins=6))

        formatter = mticker.ScalarFormatter(useMathText=True)
        formatter.set_scientific(True)
        formatter.set_powerlimits((-3, 3))
        ax.xaxis.set_major_formatter(formatter)

        fig.canvas.draw()

        # Remove zero tick
        ticks = ax.get_xticks()
        ax.set_xticks([t for t in ticks if abs(t) > 1e-12])

        # ---- MODIFY SCIENTIFIC NOTATION DIRECTLY ----
        offset_text = ax.xaxis.get_offset_text()
        offset_text.set_fontsize(OFFSET_SIZE)
        offset_text.set_fontweight("bold")
        offset_text.set_color("black")

        ax.tick_params(axis="x", labelsize=TICK_SIZE)
        ax.tick_params(axis="y", labelsize=Y_TICK_SIZE)

        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        plt.setp(ax.get_yticklabels(), rotation=30, ha="right")

        ax.grid(True, alpha=0.2, linewidth=0.5)

        if np.isfinite(p_max):
            ax.set_title(
                rf"$\hat{{p}}_{{\max}} = {self._fmt_sci_tex(float(p_max), sig=4)}$",
                fontsize=TITLE_SIZE,
                loc="left",
                pad=13,
            )

        if np.isfinite(eps_max):
            ax.axvline(
                eps_max,
                linestyle=(0, (4, 4)),
                linewidth=2.0,
                color="black",
                label=rf"$\varepsilon_{{\max}} = {self._fmt_sci_tex(float(eps_max), sig=4)}$",
            )
            ax.legend(
                loc="best",
                fontsize=AXIS_LABEL_SIZE * 0.80,
                frameon=True,
                facecolor="white",
                edgecolor="black",
                framealpha=1.0,
            )

        left = 0.18
        bottom = 0.17
        width = 0.79
        height = 0.70
        ax.set_position([left, bottom, width, height])

        if not include_y_axis_label:
            ax.set_ylabel("")
        if not include_x_axis_label:
            ax.set_xlabel("")

        if include_x_axis_label:
            ax.xaxis.set_label_coords(0.5, -0.16)
        if include_y_axis_label:
            ax.yaxis.set_label_coords(-0.155, 0.5)

        if out_path is None:
            return fig

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, format="svg")
        plt.close(fig)
        return None
        
def run_avg_bitrate_histogram(
    self,
    qdisc_root: str | Path,
    mahi_root: str | Path,
    *,
    out_path: str | Path = "./figs/avg_bitrate_validation_hist.svg",
    quantile: float = 95.0,
    include_x_axis_label: bool = False,
    include_y_axis_label: bool = False,
) -> None:
    q_ids, q_bps = self.load_group_bitrates(qdisc_root)
    m_ids, m_bps = self.load_group_bitrates(mahi_root)

    if q_bps.size == 0 or m_bps.size == 0:
        raise RuntimeError("Need non-empty samples.")

    # Mbps
    q = q_bps / 1e6
    m = m_bps / 1e6

    # diffs
    q_within = self.pairwise_abs_diffs(q)
    m_within = self.pairwise_abs_diffs(m)
    cross = self.cross_abs_diffs(q, m)

    qfinite = q_within[np.isfinite(q_within)]
    mfinite = m_within[np.isfinite(m_within)]
    cfinite = cross[np.isfinite(cross)]

    if cfinite.size == 0:
        raise RuntimeError("No cross values for validation histogram.")

    q_thr = float(np.percentile(qfinite, quantile))
    m_thr = float(np.percentile(mfinite, quantile))
    eps_min = min(q_thr, m_thr)
    eps_max = max(q_thr, m_thr)

    n = int(cfinite.size)
    p_min = float((np.sum(cfinite > eps_min) + 1) / (n + 1))
    p_max = float((np.sum(cfinite > eps_max) + 1) / (n + 1))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    self._render_validation_histogram(
        cfinite,
        eps_max,
        p_max,
        x_label="Δ Avg Throughput (Mbps)",
        include_x_axis_label=include_x_axis_label,
        include_y_axis_label=include_y_axis_label,
        out_path=out_path,
    )

    print(f"Saved: {out_path.resolve()}")

def _render_validation_histogram(
    values: np.ndarray,
    eps_max: float,
    p_max: float,
    *,
    x_label: str,
    include_x_axis_label: bool,
    include_y_axis_label: bool,
    out_path: str | Path | None = None,
):
    """
    Module-level wrapper so other modules (e.g. timeseries_validation.py) can reuse
    the exact same rendering logic without importing a class-private method.
    """
    return AvgBitrateValidation()._render_validation_histogram(
        values=values,
        eps_max=eps_max,
        p_max=p_max,
        x_label=x_label,
        include_x_axis_label=include_x_axis_label,
        include_y_axis_label=include_y_axis_label,
        out_path=out_path,
    )