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

# File name -> run id extraction
_MAHI_ID_RE = re.compile(r"^output_.*?(\d+)(?:\.\w+)?$")
_QDISC_ID_RE = re.compile(r"^qdisc_.*?(\d+)(?:\.\w+)?$")
_IPERF_SERVER_RE = re.compile(r"^iperf3_(\d+)\..*\.server\.log$")

# Mahimahi log lines: value is ALREADY milliseconds (no unit token; just a number)
_MAHI_QDELAY_L_RE = re.compile(r"\bqdelay_l_ms=([0-9]*\.?[0-9]+)\b")
_MAHI_QDELAY_C_RE = re.compile(r"\bqdelay_c_ms=([0-9]*\.?[0-9]+)\b")

# Mahimahi cumulative ECN marks (cumulative counter)
_MAHI_ECN_MARK_RE = re.compile(r"\becn_mark=([0-9]+)\b")

# Kernel/qdisc log lines: value + unit token (convert to ms)
_QDISC_DELAY_C_RE = re.compile(r"\bdelay_c\s+([0-9]*\.?[0-9]+)\s*([a-zA-Zµ]+)\b")
_QDISC_DELAY_L_RE = re.compile(r"\bdelay_l\s+([0-9]*\.?[0-9]+)\s*([a-zA-Zµ]+)\b")

# Kernel/qdisc cumulative ECN marks (cumulative counter)
_QDISC_ECN_MARK_RE = re.compile(r"\becn_mark\s+([0-9]+)\b")

# Unit conversion (to milliseconds)
_UNIT_TO_MS = {
    "ns": 1e-6,
    "us": 1e-3,
    "µs": 1e-3,
    "ms": 1.0,
    "s": 1e3,
    "sec": 1e3,
    "secs": 1e3,
}

# ============================================================
# File/id helpers
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


# ============================================================
# Parsing helpers (averages + cumulative counters)
# ============================================================

def _avg_from_matches(path: Path, rx: re.Pattern[str]) -> float:
    vals: list[float] = []
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = rx.search(line)
            if m:
                vals.append(float(m.group(1)))
    return float(np.mean(vals)) if vals else float("nan")


def _avg_from_matches_to_ms(path: Path, rx: re.Pattern[str]) -> float:
    vals_ms: list[float] = []
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = rx.search(line)
            if not m:
                continue
            val = float(m.group(1))
            unit = m.group(2).strip().lower()
            mult = _UNIT_TO_MS.get(unit)
            if mult is None:
                continue
            vals_ms.append(val * mult)
    return float(np.mean(vals_ms)) if vals_ms else float("nan")


def _nth_from_end_int_and_count(
    path: Path,
    rx: re.Pattern[str],
    *,
    nth_from_end: int = 5,
) -> tuple[float, int]:
    """
    For cumulative counters like ecn_mark, return:

        (Nth-from-last value, total_samples_seen)

    Example:
        nth_from_end=5  → 5th-from-last valid match

    If fewer than nth_from_end matches exist,
    returns the last available match.

    If none found → (NaN, 0)
    """

    values: list[int] = []

    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = rx.search(line)
            if m:
                values.append(int(m.group(1)))

    if not values:
        return (float("nan"), 0)

    total_samples = len(values)

    if total_samples >= nth_from_end:
        return (float(values[-nth_from_end]), total_samples)

    # fallback: use last available
    return (float(values[-1]), total_samples)

# ============================================================
# Numeric helpers
# ============================================================

def _sort_xy(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x)
    y = np.asarray(y)
    order = np.argsort(x)
    return x[order], y[order]


def _drop_outliers_queue_delay(y: np.ndarray, *, max_ms: float = 400.0) -> np.ndarray:
    y = np.asarray(y, dtype=np.float64).copy()
    y[np.isfinite(y) & (y > max_ms)] = np.nan
    return y


def _finite(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=np.float64)
    return y[np.isfinite(y)]


def _empirical_cdf(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xs = np.sort(_finite(x))
    n = xs.size
    if n == 0:
        return xs, xs
    y = np.arange(1, n + 1, dtype=np.float64) / float(n)
    return xs, y


def _empirical_ccdf(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xs = np.sort(_finite(x))
    n = xs.size
    if n == 0:
        return xs, xs
    y = (n - np.arange(0, n, dtype=np.float64)) / float(n)
    return xs, y


# ============================================================
# Jitter helper
# ============================================================

def _jitter_x(x: np.ndarray, *, frac: float = 0.005, seed: int = 0) -> np.ndarray:
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
# iperf helpers
# ============================================================

def _parse_iperf_bitrate(path: Path) -> float:
    """
    Parse the last 'receiver' bitrate from an iperf3 server log.
    Returns bits/sec (float) or NaN if not found.
    """
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


def _parse_iperf_receiver_transfer_bytes(path: Path) -> float:
    """
    Parse the final receiver TRANSFER from an iperf3 server log.
    Example line:
      [  5]   0.00-30.04  sec  77.9 MBytes  21.7 Mbits/sec  receiver

    Returns bytes (float) or NaN if not found.
    """
    rx = re.compile(
        r"\s([0-9]*\.?[0-9]+)\s*(Bytes|KBytes|MBytes|GBytes|TBytes)\s+"
        r"[0-9]*\.?[0-9]+\s*(?:bits/sec|Kbits/sec|Mbits/sec|Gbits/sec)\s+receiver\b",
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
                    "bytes": 1.0,
                    "kbytes": 1024.0,
                    "mbytes": 1024.0**2,
                    "gbytes": 1024.0**3,
                    "tbytes": 1024.0**4,
                }[unit]
                last = val * mult
    return float(last) if last is not None else float("nan")


def load_group_bitrates(root: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    For each run-id, sum receiver bitrates across classic+l4s server logs.
    Returns (ids, total_bits_per_sec).
    """
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


def load_group_transfer_bytes(root: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    For each run-id, sum receiver TRANSFER bytes across classic+l4s server logs.
    Returns (ids, total_bytes).
    """
    ids = _collect_ids(root, _IPERF_SERVER_RE)
    vals: list[float] = []

    for rid in ids:
        total = 0.0
        for mode in ("classic", "l4s"):
            fname = f"iperf3_{rid}.{mode}.server.log"
            for p in Path(root).rglob(fname):
                v = _parse_iperf_receiver_transfer_bytes(p)
                if np.isfinite(v):
                    total += v
        vals.append(float(total))

    return np.asarray(ids, dtype=np.int64), np.asarray(vals, dtype=np.float64)


def _transfer_bytes_by_id(root: Path) -> dict[int, float]:
    ids, b = load_group_transfer_bytes(root)
    out: dict[int, float] = {}
    for rid, v in zip(ids.tolist(), b.tolist()):
        out[int(rid)] = float(v)
    return out


# ============================================================
# Overlay Plot Class
# ============================================================

EcnNorm = Literal["raw", "per_30s", "per_sample", "per_pkt"]

@dataclass
class Overlay_Plot:
    """
    variable:
      - "qdelay_l"
      - "qdelay_c"
      - "iperf"
      - "ecn_mark"

    ECN normalization:
      - ecn_norm="raw"       : last cumulative ecn_mark (total marks)
      - ecn_norm="per_30s"   : last cumulative / ecn_div_seconds
      - ecn_norm="per_sample": last cumulative / (# ecn_mark samples observed)
      - ecn_norm="per_pkt"   : last cumulative / packets_received
                              where packets_received := (iperf receiver transfer bytes) / mtu_bytes
                              i.e. marks per packet = marks / (transfer_bytes / mtu_bytes)
    """
    variable: Literal["qdelay_l", "qdelay_c", "iperf", "ecn_mark"]
    thresh_1_root: Path
    thresh_5_root: Path
    thresh_10_root: Path
    kernel_root: Path
    bdp: str = ""

    # ECN normalization controls
    ecn_norm: EcnNorm = "raw"
    ecn_div_seconds: float = 30.0   # per_30s
    mtu_bytes: float = 1500.0       # per_pkt

    def _apply_ecn_norm(
        self,
        last_marks: float,
        n_samples: int,
        *,
        transfer_bytes: float | None = None,
    ) -> float:
        if not np.isfinite(last_marks):
            return float("nan")

        if self.ecn_norm == "raw":
            return float(last_marks)

        if self.ecn_norm == "per_30s":
            denom = float(self.ecn_div_seconds) if self.ecn_div_seconds > 0 else float("nan")
            return float(last_marks) / denom if np.isfinite(denom) else float("nan")

        if self.ecn_norm == "per_sample":
            if n_samples <= 0:
                return float("nan")
            return float(last_marks) / float(n_samples)

        # per_pkt
        if transfer_bytes is None or not np.isfinite(transfer_bytes):
            return float("nan")
        if self.mtu_bytes <= 0:
            return float("nan")
        pkts = float(transfer_bytes) / float(self.mtu_bytes)
        if pkts <= 0:
            return float("nan")
        return float(last_marks) / pkts

    def _series_mahi(self, root: Path) -> tuple[np.ndarray, np.ndarray]:
        root = Path(root)

        if self.variable == "iperf":
            ids, vals_bps = load_group_bitrates(root)
            return ids, (vals_bps / 1e6).astype(np.float64)

        transfer_map = _transfer_bytes_by_id(root) if (self.variable == "ecn_mark" and self.ecn_norm == "per_pkt") else {}

        ids = _collect_ids(root, _MAHI_ID_RE)
        y: list[float] = []

        for rid in ids:
            p = _find_by_id(root, _MAHI_ID_RE, rid)
            if p is None:
                y.append(float("nan"))
                continue

            if self.variable == "qdelay_l":
                y.append(_avg_from_matches(p, _MAHI_QDELAY_L_RE))
            elif self.variable == "qdelay_c":
                y.append(_avg_from_matches(p, _MAHI_QDELAY_C_RE))
            else:
                last_marks, n = _nth_from_end_int_and_count(
                    p,
                    _MAHI_ECN_MARK_RE,
                    nth_from_end=5,
                )                
                tb = transfer_map.get(int(rid)) if transfer_map else None
                y.append(self._apply_ecn_norm(last_marks, n, transfer_bytes=tb))

        y_arr = np.asarray(y, dtype=np.float64)
        if self.variable in ("qdelay_l", "qdelay_c"):
            y_arr = _drop_outliers_queue_delay(y_arr, max_ms=400.0)

        return np.asarray(ids, dtype=np.int64), y_arr

    def _series_kernel(self) -> tuple[np.ndarray, np.ndarray]:
        root = Path(self.kernel_root)

        if self.variable == "iperf":
            ids, vals_bps = load_group_bitrates(root)
            return ids, (vals_bps / 1e6).astype(np.float64)

        transfer_map = _transfer_bytes_by_id(root) if (self.variable == "ecn_mark" and self.ecn_norm == "per_pkt") else {}

        ids = _collect_ids(root, _QDISC_ID_RE)
        y: list[float] = []

        for rid in ids:
            p = _find_by_id(root, _QDISC_ID_RE, rid)
            if p is None:
                y.append(float("nan"))
                continue

            if self.variable == "qdelay_l":
                y.append(_avg_from_matches_to_ms(p, _QDISC_DELAY_L_RE))
            elif self.variable == "qdelay_c":
                y.append(_avg_from_matches_to_ms(p, _QDISC_DELAY_C_RE))
            else:
                last_marks, n = _nth_from_end_int_and_count(
                    p,
                    _QDISC_ECN_MARK_RE,
                    nth_from_end=5,
                )  
                tb = transfer_map.get(int(rid)) if transfer_map else None
                y.append(self._apply_ecn_norm(last_marks, n, transfer_bytes=tb))

        y_arr = np.asarray(y, dtype=np.float64)
        if self.variable in ("qdelay_l", "qdelay_c"):
            y_arr = _drop_outliers_queue_delay(y_arr, max_ms=400.0)

        return np.asarray(ids, dtype=np.int64), y_arr

    def _plot_series_connected(self, ax, x: np.ndarray, y: np.ndarray, *, marker: str, label: str):
        x, y = _sort_xy(x, y)
        ax.plot(x, y, marker=marker, linestyle="-", label=label, alpha=0.85)

    def _ecn_label(self) -> str:
        if self.ecn_norm == "raw":
            return "ECN marks (total, cumulative)"
        if self.ecn_norm == "per_30s":
            return f"ECN marks / {self.ecn_div_seconds:g}s"
        if self.ecn_norm == "per_sample":
            return "ECN marks / sample"
        return f"ECN marks / pkt (pkts≈transfer/{self.mtu_bytes:g}B)"

    def _y_label(self) -> str:
        if self.variable == "iperf":
            return "avg bitrate (Mbps)"
        if self.variable in ("qdelay_l", "qdelay_c"):
            return f"avg {self.variable} (ms)"
        return self._ecn_label()

    def _x_label_for_cdf(self) -> str:
        if self.variable == "iperf":
            return "avg bitrate (Mbps)"
        if self.variable in ("qdelay_l", "qdelay_c"):
            return f"avg {self.variable} (ms)"
        return self._ecn_label()

    def plot(self, out_path: Path | None = None):
        plt.rcParams.update({"font.size": 16})
        fig, ax = plt.subplots(figsize=(8.5, 5))

        x1, y1 = self._series_mahi(self.thresh_1_root)
        x5, y5 = self._series_mahi(self.thresh_5_root)
        x10, y10 = self._series_mahi(self.thresh_10_root)
        xk, yk = self._series_kernel()

        self._plot_series_connected(ax, x1, y1, marker="x", label="thresh 1ms")
        self._plot_series_connected(ax, x5, y5, marker="o", label="thresh 5ms")
        self._plot_series_connected(ax, x10, y10, marker="^", label="thresh 10ms")
        self._plot_series_connected(ax, xk, yk, marker="s", label="kernel")

        ax.set_xlabel("run id")
        ax.set_ylabel(self._y_label())

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

        # Wider + slightly taller
        fig, ax = plt.subplots(figsize=(10.5, 6.8))

        # ----------------------------------------------------
        # Collect values
        # ----------------------------------------------------
        _, y1 = self._series_mahi(self.thresh_1_root)
        _, y5 = self._series_mahi(self.thresh_5_root)
        _, y10 = self._series_mahi(self.thresh_10_root)
        _, yk = self._series_kernel()

        fn = _empirical_ccdf if ccdf else _empirical_cdf
        x_1, p_1 = fn(y1)
        x_5, p_5 = fn(y5)
        x_10, p_10 = fn(y10)
        x_k, p_k = fn(yk)

        # ----------------------------------------------------
        # Add small horizontal jitter
        # ----------------------------------------------------
        x_1 = _jitter_x(x_1, frac=0.004, seed=1)
        x_5 = _jitter_x(x_5, frac=0.004, seed=2)
        x_10 = _jitter_x(x_10, frac=0.004, seed=3)
        x_k = _jitter_x(x_k, frac=0.004, seed=4)

        def auto_markevery(x):
            return max(1, len(x) // 10)

        # ----------------------------------------------------
        # Plot lines (NO percent scaling)
        # ----------------------------------------------------
        ax.plot(
            x_1, p_1,
            marker="x", linestyle="-",
            linewidth=1.5, markersize=14,
            markevery=auto_markevery(x_1),
            label="thresh 1ms",
        )
        ax.plot(
            x_5, p_5,
            marker="o", linestyle="-",
            linewidth=1.5, markersize=14,
            markevery=auto_markevery(x_5),
            label="thresh 5ms",
        )
        ax.plot(
            x_10, p_10,
            marker="^", linestyle="-",
            linewidth=1.5, markersize=14,
            markevery=auto_markevery(x_10),
            label="thresh 10ms",
        )
        ax.plot(
            x_k, p_k,
            marker="s", linestyle="-",
            linewidth=1.5, markersize=14,
            markevery=auto_markevery(x_k),
            label="kernel",
            alpha=0.65,
        )

        # ----------------------------------------------------
        # Labels
        # ----------------------------------------------------
        xlab = self._x_label_for_cdf()
        if xlab.strip() == "avg bitrate (Mbps)":
            xlab = "Average Bitrate (Mbps)"

        ax.set_xlabel(xlab, fontsize=38)
        ax.tick_params(axis="both", labelsize=24, width=1.5)
        ax.tick_params(axis="x", rotation=30)

        if show_y_axis:
            ax.set_ylabel("Cumulative Probability", fontsize=36)
            ax.tick_params(axis="y", labelleft=True, left=True)
            ax.spines["left"].set_visible(True)
        else:
            # hide y axis labels/ticks but keep a left border if you want the frame
            ax.set_ylabel("")
            ax.tick_params(axis="y", labelleft=False, left=False)
            ax.spines["left"].set_visible(True)

        # Probabilities are in [0,1]
        ax.set_ylim(0.0, 1.0)

        ax.grid(alpha=0.25)

        # ----------------------------------------------------
        # Layout + Legend
        # ----------------------------------------------------
        if show_y_axis:
            fig.subplots_adjust(left=0.12, bottom=0.20, right=0.98, top=0.96)
            ax.legend(fontsize=36, frameon=True, loc="best")
        else:
            fig.subplots_adjust(left=0.02, bottom=0.18, right=0.98, top=0.96)

        # ----------------------------------------------------
        # Save
        # ----------------------------------------------------
        if out_path is not None:
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(out_path)
            fig.savefig(out_path.with_suffix(".svg"))

        return fig, ax