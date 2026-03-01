#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import re

import numpy as np
import matplotlib.pyplot as plt


# ============================================================
# Regex definitions
# ============================================================

_MAHI_ID_RE = re.compile(r"^output_.*?(\d+)(?:\.\w+)?$")
_QDISC_ID_RE = re.compile(r"^qdisc_.*?(\d+)(?:\.\w+)?$")

_MAHI_QDELAY_L_RE = re.compile(r"\bqdelay_l_ms=([0-9]*\.?[0-9]+)")
_MAHI_QDELAY_C_RE = re.compile(r"\bqdelay_c_ms=([0-9]*\.?[0-9]+)")

_QDISC_DELAY_C_RE = re.compile(r"\bdelay_c\s+(\d+)\s*us\b")
_QDISC_DELAY_L_RE = re.compile(r"\bdelay_l\s+(\d+)\s*us\b")

_IPERF_SERVER_RE = re.compile(r"^iperf3_(\d+)\..*\.server\.log$")

# ============================================================
# Jitter helper
# ============================================================

def _jitter_x(x: np.ndarray, *, frac: float = 0.005, seed: int = 0) -> np.ndarray:
    """
    Add small horizontal jitter to x values.

    frac = fraction of data range used as jitter magnitude.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x

    rng = np.random.default_rng(seed)

    data_range = np.nanmax(x) - np.nanmin(x)
    if data_range == 0:
        return x

    magnitude = data_range * frac
    noise = rng.uniform(-magnitude, magnitude, size=x.shape)

    return x + noise

# ============================================================
# Helpers
# ============================================================

def _collect_ids(root: Path, rx: re.Pattern[str]) -> list[int]:
    ids: set[int] = set()
    for p in Path(root).rglob("*"):
        if p.is_file():
            m = rx.match(p.name)
            if m:
                ids.add(int(m.group(1)))
    return sorted(ids)


def _find_by_id(root: Path, rx: re.Pattern[str], run_id: int) -> Path | None:
    for p in Path(root).rglob("*"):
        if p.is_file():
            m = rx.match(p.name)
            if m and int(m.group(1)) == run_id:
                return p
    return None


def _avg_from_matches(path: Path, rx: re.Pattern[str], scale: float = 1.0) -> float:
    # average across all matched samples in this file: sum(vals)/len(vals)
    vals: list[float] = []
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = rx.search(line)
            if m:
                vals.append(float(m.group(1)) * scale)
    return float(np.mean(vals)) if vals else float("nan")


def _sort_xy(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x)
    y = np.asarray(y)
    order = np.argsort(x)
    return x[order], y[order]


def _drop_outliers_queue_delay(y: np.ndarray, *, max_ms: float = 400.0) -> np.ndarray:
    """
    For queue delay series only:
      if avg delay > max_ms, ignore the point by turning it into NaN.
    """
    y = np.asarray(y, dtype=np.float64).copy()
    y[np.isfinite(y) & (y > max_ms)] = np.nan
    return y


def _finite(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=np.float64)
    return y[np.isfinite(y)]


def _empirical_cdf(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Empirical CDF: y = P(X <= x)
    Returns x_sorted (ascending) and y in [0,1].
    """
    xs = np.sort(_finite(x))
    n = xs.size
    if n == 0:
        return xs, xs
    y = np.arange(1, n + 1, dtype=np.float64) / float(n)
    return xs, y


def _empirical_ccdf(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Empirical CCDF / survival: y = P(X >= x)
    Returns x_sorted (ascending) and y in (0,1].
    """
    xs = np.sort(_finite(x))
    n = xs.size
    if n == 0:
        return xs, xs
    # For xs[0] (smallest), P(X >= xs[0]) = 1.0
    # For xs[-1] (largest), P(X >= xs[-1]) = 1/n
    y = (n - np.arange(0, n, dtype=np.float64)) / float(n)
    return xs, y


# ============================================================
# iperf hooks
# ============================================================

def _parse_iperf_bitrate(path: Path) -> float:
    rx = re.compile(
        r"\s([0-9]*\.?[0-9]+)\s*(bits/sec|Kbits/sec|Mbits/sec|Gbits/sec)\s+receiver",
        re.IGNORECASE,
    )
    last = None
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = rx.search(line)
            if m:
                val = float(m.group(1))
                unit = m.group(2).lower()
                mult = {
                    "bits/sec": 1.0,
                    "kbits/sec": 1e3,
                    "mbits/sec": 1e6,
                    "gbits/sec": 1e9,
                }[unit]
                last = val * mult
    return float(last) if last is not None else float("nan")


def load_group_bitrates(root: Path) -> tuple[np.ndarray, np.ndarray]:
    ids = _collect_ids(root, _IPERF_SERVER_RE)
    vals: list[float] = []

    for rid in ids:
        total = 0.0
        for mode in ("classic", "l4s"):
            fname = f"iperf3_{rid}.{mode}.server.log"
            for p in Path(root).rglob(fname):
                v = _parse_iperf_bitrate(p)
                if np.isfinite(v):
                    total += v
        vals.append(float(total))

    return np.asarray(ids, dtype=np.int64), np.asarray(vals, dtype=np.float64)

# ============================================================
# NEW: Overlay Plot Class (target_1/30/45 + kernel)
#   - Markers match thresh-version:
#       target 1ms  -> "x"
#       target 30ms -> "o"
#       target 45ms -> "^"
#       kernel      -> "s"
# ============================================================

@dataclass
class Overlay_Plot_Target:
    variable: Literal["qdelay_l", "qdelay_c", "iperf"]
    target_1_root: Path
    target_30_root: Path
    target_45_root: Path
    kernel_root: Path
    bdp: str = ""

    def _series_mahi(self, root: Path) -> tuple[np.ndarray, np.ndarray]:
        root = Path(root)

        if self.variable == "iperf":
            ids, vals_bps = load_group_bitrates(root)
            y = (vals_bps / 1e6).astype(np.float64)  # Mbps
            return ids, y

        ids = _collect_ids(root, _MAHI_ID_RE)
        y: list[float] = []
        for rid in ids:
            p = _find_by_id(root, _MAHI_ID_RE, rid)
            if p is None:
                y.append(float("nan"))
                continue

            if self.variable == "qdelay_l":
                y.append(_avg_from_matches(p, _MAHI_QDELAY_L_RE, 1.0))
            else:
                y.append(_avg_from_matches(p, _MAHI_QDELAY_C_RE, 1.0))

        y_arr = np.asarray(y, dtype=np.float64)
        y_arr = _drop_outliers_queue_delay(y_arr, max_ms=400.0)
        return np.asarray(ids, dtype=np.int64), y_arr

    def _series_kernel(self) -> tuple[np.ndarray, np.ndarray]:
        root = Path(self.kernel_root)

        if self.variable == "iperf":
            ids, vals_bps = load_group_bitrates(root)
            y = (vals_bps / 1e6).astype(np.float64)  # Mbps
            return ids, y

        ids = _collect_ids(root, _QDISC_ID_RE)
        y: list[float] = []
        for rid in ids:
            p = _find_by_id(root, _QDISC_ID_RE, rid)
            if p is None:
                y.append(float("nan"))
                continue

            # kernel is microseconds -> ms
            if self.variable == "qdelay_l":
                y.append(_avg_from_matches(p, _QDISC_DELAY_L_RE, 1.0 / 1000.0))
            else:
                y.append(_avg_from_matches(p, _QDISC_DELAY_C_RE, 1.0 / 1000.0))

        y_arr = np.asarray(y, dtype=np.float64)
        y_arr = _drop_outliers_queue_delay(y_arr, max_ms=400.0)
        return np.asarray(ids, dtype=np.int64), y_arr

    def _plot_series_connected(self, ax, x: np.ndarray, y: np.ndarray, *, marker: str, label: str):
        x, y = _sort_xy(x, y)
        ax.plot(x, y, marker=marker, linestyle="-", label=label)

    def plot(self, out_path: Path | None = None):
        """
        Overlay plot vs run id (connected dots).
        """
        plt.rcParams.update({"font.size": 16})
        fig, ax = plt.subplots(figsize=(8.5, 5))

        x1, y1 = self._series_mahi(self.target_1_root)
        x30, y30 = self._series_mahi(self.target_30_root)
        x45, y45 = self._series_mahi(self.target_45_root)
        xk, yk = self._series_kernel()

        self._plot_series_connected(ax, x1, y1, marker="x", label="target 1ms")
        self._plot_series_connected(ax, x30, y30, marker="o", label="target 30ms")
        self._plot_series_connected(ax, x45, y45, marker="^", label="target 45ms")
        self._plot_series_connected(ax, xk, yk, marker="s", label="kernel")

        ax.set_xlabel("run id")
        ylabel = "avg bitrate (Mbps)" if self.variable == "iperf" else f"avg {self.variable} (ms)"
        ax.set_ylabel(ylabel)

        ax.tick_params(axis="x", rotation=30)
        ax.tick_params(axis="y", rotation=30)

        ax.legend()
        fig.tight_layout()

        if out_path is not None:
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(out_path)
            fig.savefig(out_path.with_suffix(".svg"))

        return fig, ax

    def plot_cdf(
        self,
        out_path: Path | None = None,
        *,
        ccdf: bool = True,
        show_y_axis: bool = True,
    ):
        """
        CDF plot over VALUES (not run id).

        - ccdf=True  : plot P(X >= x)
        - ccdf=False : plot P(X <= x)
        - show_y_axis: toggle y-axis visibility
        """
        plt.rcParams.update({"font.size": 18})
        fig, ax = plt.subplots(figsize=(9, 6))

        # ----------------------------------------------------
        # Collect values
        # ----------------------------------------------------
        _, y1 = self._series_mahi(self.target_1_root)
        _, y30 = self._series_mahi(self.target_30_root)
        _, y45 = self._series_mahi(self.target_45_root)
        _, yk = self._series_kernel()  # <-- add kernel back

        fn = _empirical_ccdf if ccdf else _empirical_cdf
        x_1, p_1 = fn(y1)
        x_30, p_30 = fn(y30)
        x_45, p_45 = fn(y45)
        x_k, p_k = fn(yk)

        # ----------------------------------------------------
        # Add small horizontal jitter
        # ----------------------------------------------------
        x_1 = _jitter_x(x_1, frac=0.004, seed=1)
        x_30 = _jitter_x(x_30, frac=0.004, seed=2)
        x_45 = _jitter_x(x_45, frac=0.004, seed=3)
        x_k = _jitter_x(x_k, frac=0.004, seed=4)

        def auto_markevery(x):
            return max(1, len(x) // 10)

        # ----------------------------------------------------
        # Plot lines (NO percent scaling)
        # ----------------------------------------------------
        ax.plot(
            x_1, p_1,
            marker="x", linestyle="-",
            linewidth=1.5, markersize=12,
            markevery=auto_markevery(x_1),
            label="Target 1ms",
        )
        ax.plot(
            x_30, p_30,
            marker="o", linestyle="-",
            linewidth=1.5, markersize=12,
            markevery=auto_markevery(x_30),
            label="Target 30ms",
        )
        ax.plot(
            x_45, p_45,
            marker="^", linestyle="-",
            linewidth=1.5, markersize=12,
            markevery=auto_markevery(x_45),
            label="Target 45ms",
        )
        ax.plot(
            x_k, p_k,
            marker="s", linestyle="-",
            linewidth=1.5, markersize=12,
            markevery=auto_markevery(x_k),
            label="Kernel",
        )

        # ----------------------------------------------------
        # Labels
        # ----------------------------------------------------
        xlab = "Average bitrate (Mbps)" if self.variable == "iperf" else f"Average {self.variable} (ms)"
        ax.set_xlabel(xlab, fontsize=38)
        ax.tick_params(axis="both", labelsize=24, width=1.5)
        ax.tick_params(axis="x", rotation=30)

        # ----------------------------------------------------
        # Y axis
        # ----------------------------------------------------
        if show_y_axis:
            ax.set_ylabel("Cumulative Probability", fontsize=30)
            ax.tick_params(axis="y", labelleft=True, left=True)
            ax.spines["left"].set_visible(True)
            ax.legend(fontsize=30, frameon=True, loc="best")  # <-- add legend back
        else:
            ax.set_ylabel("")
            ax.tick_params(axis="y", labelleft=False, left=False)
            ax.spines["left"].set_visible(True)  # keep border even if y-axis hidden

        ax.grid(alpha=0.25)

        # ----------------------------------------------------
        # Layout (full manual control)
        # ----------------------------------------------------
        if show_y_axis:
            left = 0.13
        else:
            left = 0.02  # still leave a hair so spine isn't flush-clipped

        bottom = 0.22
        right = 0.995
        top = 0.98
        fig.subplots_adjust(left=left, bottom=bottom, right=right, top=top)

        # ----------------------------------------------------
        # Save
        # ----------------------------------------------------
        if out_path is not None:
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(out_path)
            fig.savefig(out_path.with_suffix(".svg"))

        return fig, ax

@dataclass
class Overlay_Plot_BDP:
    variable: Literal["qdelay_l", "qdelay_c", "iperf"]

    # mahimahi / bdp roots
    bdp_12_root: Path
    bdp_50_root: Path
    bdp_200_root: Path

    # kernel / bdp roots (NEW)
    kernel_bdp_12_root: Path
    kernel_bdp_50_root: Path
    kernel_bdp_200_root: Path

    bdp: str = ""

    def _series_mahi(self, root: Path) -> tuple[np.ndarray, np.ndarray]:
        root = Path(root)

        if self.variable == "iperf":
            ids, vals_bps = load_group_bitrates(root)
            y = (vals_bps / 1e6).astype(np.float64)  # Mbps
            return ids, y

        ids = _collect_ids(root, _MAHI_ID_RE)
        y: list[float] = []
        for rid in ids:
            p = _find_by_id(root, _MAHI_ID_RE, rid)
            if p is None:
                y.append(float("nan"))
                continue

            if self.variable == "qdelay_l":
                y.append(_avg_from_matches(p, _MAHI_QDELAY_L_RE, 1.0))
            else:
                y.append(_avg_from_matches(p, _MAHI_QDELAY_C_RE, 1.0))

        y_arr = np.asarray(y, dtype=np.float64)
        y_arr = _drop_outliers_queue_delay(y_arr, max_ms=400.0)
        return np.asarray(ids, dtype=np.int64), y_arr

    # kernel uses same parsing logic as mahi (same file formats/regexes)
    def _series_kernel(self, root: Path) -> tuple[np.ndarray, np.ndarray]:
        return self._series_mahi(root)

    def plot_cdf_3panel(self, out_path: Path | None = None):
        """
        Generate 3 separate CDF plots (one per BDP root) and overlay kernel in red squares.

        Plot 1:
            - Full y-axis (label + ticks)

        Plot 2 and 3:
            - No y-axis label
            - No y tick labels
            - No y tick marks
            - KEEP horizontal gridlines
            - Tight left margin

        Always CDF.
        No legends.
        """

        plt.rcParams.update({"font.size": 18})

        roots = [
            ("bdp12", self.bdp_12_root, self.kernel_bdp_12_root, 1),
            ("bdp50", self.bdp_50_root, self.kernel_bdp_50_root, 2),
            ("bdp200", self.bdp_200_root, self.kernel_bdp_200_root, 3),
        ]

        fn = _empirical_cdf  # always CDF

        xlab = (
            "Average bitrate (Mbps)"
            if self.variable == "iperf"
            else f"Average {self.variable} (ms)"
        )
        ylab = "Cumulative Probability"

        def auto_markevery(x: np.ndarray) -> int:
            return max(1, len(x) // 10)

        figs = []
        axs = []

        for idx, (suffix, mahi_root, kernel_root, seed) in enumerate(roots):
            fig, ax = plt.subplots(figsize=(6.6, 5.6))

            # -------------------------
            # Mahimahi series (default)
            # -------------------------
            _, y_m = self._series_mahi(mahi_root)
            x_m, p_m = fn(y_m)
            x_m = _jitter_x(x_m, frac=0.004, seed=seed)

            # Mahimahi
            ax.plot(
                x_m,
                p_m,
                marker="o",
                linestyle="-",
                linewidth=1.8,
                markersize=11,
                markevery=auto_markevery(x_m),
                label="Mahimahi",
            )

            # -------------------------
            # Kernel overlay (RED squares)
            # -------------------------
            _, y_k = self._series_kernel(kernel_root)
            x_k, p_k = fn(y_k)
            x_k = _jitter_x(x_k, frac=0.004, seed=1000 + seed)

            # Kernel (red squares)
            ax.plot(
                x_k,
                p_k,
                marker="s",
                linestyle="-",
                linewidth=1.8,
                markersize=11,
                markevery=auto_markevery(x_k),
                color="red",
                alpha=0.8,
                label="Kernel",
            )

            ax.set_xlabel(xlab, fontsize=32)

            if idx == 0:
                ax.set_ylabel(ylab, fontsize=28)
                ax.legend(
                    fontsize=28,
                    frameon=True,          # turn box on
                    fancybox=True,         # rounded corners
                    framealpha=0.95,       # solid box
                    edgecolor="black",     # border color
                    facecolor="white",     # background color
                )
                fig.subplots_adjust(left=0.17, right=0.98, bottom=0.20, top=0.97)
            else:
                ax.set_ylabel("")
                ax.tick_params(axis="y", which="both", left=False, labelleft=False)
                fig.subplots_adjust(left=0.06, right=0.98, bottom=0.20, top=0.97)

            ax.tick_params(axis="both", labelsize=18, width=1.4)
            ax.tick_params(axis="x", rotation=30)

            ax.grid(True, alpha=0.25)

            for spine in ax.spines.values():
                spine.set_linewidth(1.2)

            fig.tight_layout(pad=0.2)

            if out_path is not None:
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                single_path = out_path.with_name(f"{out_path.stem}_{suffix}{out_path.suffix}")
                fig.savefig(single_path)

            figs.append(fig)
            axs.append(ax)

        return figs, axs