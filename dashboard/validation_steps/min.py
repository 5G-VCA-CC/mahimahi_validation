# validation_steps/min.py
from __future__ import annotations

from pathlib import Path
import re
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# iperf3 server summary parsing
# ============================================================
_SUMMARY_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s+sec\s+"
    r"(\d+(?:\.\d+)?)\s+([KMGTP]?Bytes)\s+"
    r"(\d+(?:\.\d+)?)\s+([KMGTP]?bits/sec)\s+"
    r".*\breceiver\b"
)

_UNIT_BITS = {
    "bits/sec": 1.0,
    "Kbits/sec": 1e3,
    "Mbits/sec": 1e6,
    "Gbits/sec": 1e9,
    "Tbits/sec": 1e12,
    "Pbits/sec": 1e15,
}

_ID_RE = re.compile(r"^iperf3_(\d+)\.(classic|l4s)\.server\.log$")


def _parse_avg_bitrate_bps_from_server_log(path: Path) -> float:
    try:
        txt = path.read_text(errors="ignore")
    except FileNotFoundError:
        return 0.0

    last = None
    for line in txt.splitlines():
        m = _SUMMARY_RE.match(line.strip())
        if not m:
            continue
        br = float(m.group(5))
        unit = m.group(6)
        last = br * _UNIT_BITS.get(unit, 1.0)

    return 0.0 if last is None else float(last)


# ============================================================
# Discover runs
# ============================================================
def _collect_run_ids(root: Path) -> list[int]:
    ids = set()
    for p in Path(root).rglob("iperf3_*.server.log"):
        m = _ID_RE.match(p.name)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


def _find_one(root: Path, filename: str) -> Path | None:
    for p in Path(root).rglob(filename):
        return p
    return None


def _total_bitrate_bps_for_id(root: Path, run_id: int) -> float:
    classic_name = f"iperf3_{run_id}.classic.server.log"
    l4s_name     = f"iperf3_{run_id}.l4s.server.log"

    classic = _find_one(root, classic_name)
    l4s     = _find_one(root, l4s_name)

    total = 0.0
    if classic is not None:
        total += _parse_avg_bitrate_bps_from_server_log(classic)
    if l4s is not None:
        total += _parse_avg_bitrate_bps_from_server_log(l4s)
    return float(total)


def load_group_bitrates(root: str | Path) -> tuple[np.ndarray, np.ndarray]:
    root = Path(root)
    ids = _collect_run_ids(root)
    vals = [_total_bitrate_bps_for_id(root, rid) for rid in ids]
    return np.asarray(ids, dtype=np.int64), np.asarray(vals, dtype=np.float64)


# ============================================================
# |Δ| distributions
# ============================================================
def pairwise_abs_diffs(x: np.ndarray) -> np.ndarray:
    n = x.size
    if n < 2:
        return np.asarray([], dtype=np.float64)
    vals = []
    for i in range(n):
        for j in range(i + 1, n):
            vals.append(abs(x[i] - x[j]))
    return np.asarray(vals, dtype=np.float64)


def cross_abs_diffs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if a.size == 0 or b.size == 0:
        return np.asarray([], dtype=np.float64)
    vals = []
    for ai in a:
        for bj in b:
            vals.append(abs(ai - bj))
    return np.asarray(vals, dtype=np.float64)


# ============================================================
# Plotting
# ============================================================
def plot_triple_hist_scalar(q_within, m_within, cross, *, quantile=95.0):

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
    axes[0].set_title(f"Qdisc |Δ| (Mbps)\nq{quantile:g}={q_thr:.2f}")

    axes[1].hist(m_within, bins=30, edgecolor="black")
    axes[1].axvline(m_thr, linestyle="--")
    axes[1].set_title(f"Mahimahi |Δ| (Mbps)\nq{quantile:g}={m_thr:.2f}")

    axes[2].hist(cross, bins=30, edgecolor="black")
    axes[2].axvline(eps_min, linestyle="--")
    axes[2].axvline(eps_max, linestyle="--")
    axes[2].set_title(
        f"Cross |Δ| (Mbps)\n"
        f"eps_min={eps_min:.2f}, p_min={p_min:.4f} | "
        f"eps_max={eps_max:.2f}, p_max={p_max:.4f}"
    )

    for ax in axes:
        ax.set_xlabel("|Δ bitrate| (Mbps)")
        ax.grid(True, alpha=0.2)

    axes[0].set_ylabel("count")
    plt.tight_layout()
    return fig


# ============================================================
# Public entrypoint
# ============================================================
def run_iperf_totalreceived_histogram(
    qdisc_root: str | Path,
    mahi_root: str | Path,
    *,
    out_path: str | Path = "./figs/iperf_avg_bitrate_triple_hist.svg",
    quantile: float = 95.0,
):

    q_ids, q = load_group_bitrates(qdisc_root)
    m_ids, m = load_group_bitrates(mahi_root)

    if q.size == 0 or m.size == 0:
        raise RuntimeError(f"Need non-empty samples. qdisc={q.size}, mahi={m.size}")

    # Convert to Mbps
    q = q / 1e6
    m = m / 1e6

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # -------- Plot per-run bitrate (Mbps) --------
    runs_path = out_path.with_name(out_path.stem.replace("triple_hist", "per_run") + out_path.suffix)

    fig_runs = plt.figure(figsize=(10, 4))
    plt.plot(q_ids, q, marker="o", label="qdisc (linux)")
    plt.plot(m_ids, m, marker="o", label="mahimahi")
    plt.xlabel("run id")
    plt.ylabel("avg bitrate (Mbps)")
    plt.title("IPERF_AVG_BITRATE — avg receiver bitrate per run")

    # Disable scientific notation
    plt.ticklabel_format(style="plain", axis="y")

    plt.grid(True, alpha=0.2)
    plt.legend()
    plt.tight_layout()
    fig_runs.savefig(runs_path, dpi=200, bbox_inches="tight")
    plt.close(fig_runs)

    # -------- Plot triple histogram --------
    q_within = pairwise_abs_diffs(q)
    m_within = pairwise_abs_diffs(m)
    cross = cross_abs_diffs(q, m)

    fig_hist = plot_triple_hist_scalar(q_within, m_within, cross, quantile=quantile)
    fig_hist.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig_hist)

    print(f"[IPERF_AVG_BITRATE] qdisc N={q.size}  mahi N={m.size}")
    print(f"Saved: {runs_path.resolve()}")
    print(f"Saved: {out_path.resolve()}")
