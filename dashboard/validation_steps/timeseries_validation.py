# validation_steps/validation.py
from __future__ import annotations

from pathlib import Path
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from mahimahi_validation.dashboard.validation_steps.avg_bitrate_validation import (
    _render_validation_histogram,
)

from .dtw_analyzer import DTWAnalyzer


# ============================================================
# Matplotlib defaults (only for validation/plots)
# ============================================================
plt.rcParams.update({
    "svg.fonttype": "none",
    "font.size": 11,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
})


# ============================================================
# Bootstrap helpers (run-ID resampling)
# ============================================================
def _within_dists_for_sample(keys_sample, dist_dict):
    """
    Build within-group distances for a bootstrap replicate.

    keys_sample: list[str] (length n), with replacement allowed.
    dist_dict: dict[a][b] -> dist (symmetric).
    Returns: 1D float array of pairwise distances over i<j.

    Note:
      If the same run ID repeats, we treat its self-distance as 0.0.
      Missing pairs are skipped (should be rare if cache is complete).
    """
    out = []
    n = len(keys_sample)
    for i in range(n):
        ai = keys_sample[i]
        for j in range(i + 1, n):
            bj = keys_sample[j]
            if ai == bj:
                out.append(0.0)
                continue
            d = dist_dict.get(ai, {}).get(bj)
            if d is None:
                d = dist_dict.get(bj, {}).get(ai)
            if d is None or not np.isfinite(d):
                continue
            out.append(float(d))
    return np.asarray(out, dtype=float)


def _cross_dists_for_sample(an: DTWAnalyzer, q_sample, m_sample):
    """
    Build cross-group distances for a bootstrap replicate.

    q_sample: list[str] qdisc run IDs (length nq)
    m_sample: list[str] mahi  run IDs (length nm)
    Returns: 1D float array of all cross distances.
    Missing pairs are skipped.
    """
    out = []
    for qk in q_sample:
        row = an.cross_dist.get(qk, {})
        for mk in m_sample:
            d = row.get(mk)
            if d is None or not np.isfinite(d):
                continue
            out.append(float(d))
    return np.asarray(out, dtype=float)


def bootstrap_ci_pmax(
    an: DTWAnalyzer,
    *,
    quantile=95.0,
    B=2000,
    alpha=0.05,
    seed=0,
):
    """
    Bootstrap CI for p_max using *run-ID resampling*.

    Returns:
      (p_lo, p_hi, p_upper_1s)
    """
    rng = np.random.default_rng(seed)

    q_keys = an.qdisc_keys()
    m_keys = an.mahi_keys()

    if not q_keys or not m_keys:
        return (np.nan, np.nan, np.nan)

    nq = len(q_keys)
    nm = len(m_keys)

    boots = np.empty(B, dtype=float)

    for b in range(B):
        q_s = [q_keys[i] for i in rng.integers(0, nq, size=nq)]
        m_s = [m_keys[i] for i in rng.integers(0, nm, size=nm)]

        q_within = _within_dists_for_sample(q_s, an.qdisc_dist)
        m_within = _within_dists_for_sample(m_s, an.mahi_dist)
        cross = _cross_dists_for_sample(an, q_s, m_s)

        if q_within.size == 0 or m_within.size == 0 or cross.size == 0:
            boots[b] = np.nan
            continue

        q_thr = float(np.percentile(q_within, quantile))
        m_thr = float(np.percentile(m_within, quantile))
        eps_max_b = max(q_thr, m_thr)

        n = int(cross.size)
        boots[b] = float((np.sum(cross > eps_max_b) + 1) / (n + 1))

    boots = boots[np.isfinite(boots)]
    if boots.size == 0:
        return (np.nan, np.nan, np.nan)

    p_lo, p_hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    p_upper_1s = np.quantile(boots, 1 - alpha)
    return (float(p_lo), float(p_hi), float(p_upper_1s))


def bootstrap_ci_pmax_for_keys(
    an: DTWAnalyzer,
    q_keys,
    m_keys,
    *,
    quantile=95.0,
    B=2000,
    alpha=0.05,
    seed=0,
):
    """
    Same as bootstrap_ci_pmax(), but restricted to provided key lists.
    Resamples *run IDs* (with replacement) within those subsets.
    """
    rng = np.random.default_rng(seed)

    if not q_keys or not m_keys:
        return (np.nan, np.nan, np.nan)

    nq = len(q_keys)
    nm = len(m_keys)

    boots = np.empty(B, dtype=float)

    for b in range(B):
        q_s = [q_keys[i] for i in rng.integers(0, nq, size=nq)]
        m_s = [m_keys[i] for i in rng.integers(0, nm, size=nm)]

        q_within = _within_dists_for_sample(q_s, an.qdisc_dist)
        m_within = _within_dists_for_sample(m_s, an.mahi_dist)
        cross = _cross_dists_for_sample(an, q_s, m_s)

        if q_within.size == 0 or m_within.size == 0 or cross.size == 0:
            boots[b] = np.nan
            continue

        q_thr = float(np.percentile(q_within, quantile))
        m_thr = float(np.percentile(m_within, quantile))
        eps_max_b = max(q_thr, m_thr)

        n = int(cross.size)
        boots[b] = float((np.sum(cross > eps_max_b) + 1) / (n + 1))

    boots = boots[np.isfinite(boots)]
    if boots.size == 0:
        return (np.nan, np.nan, np.nan)

    p_lo, p_hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    p_upper_1s = np.quantile(boots, 1 - alpha)
    return (float(p_lo), float(p_hi), float(p_upper_1s))


def print_bootstrap_ci_vs_n(
    an: DTWAnalyzer,
    ns=(10, 25, 40, 60, 80, 100),
    *,
    quantile=95.0,
    B=2000,
    alpha=0.05,
    seed=0,
):
    """
    For each n in ns:
    - take n runs from qdisc + n runs from mahimahi (without replacement)
    - bootstrap p_max CI within that subset (run-ID bootstrap)
    - print the CI
    """
    rng = np.random.default_rng(seed)

    q_all = list(an.qdisc_keys())
    m_all = list(an.mahi_keys())

    if not q_all or not m_all:
        print("No keys available.")
        return []

    rng.shuffle(q_all)
    rng.shuffle(m_all)

    out = []
    max_n = min(len(q_all), len(m_all))

    for n in ns:
        n_use = n if n <= max_n else max_n
        q_sub = q_all[:n_use]
        m_sub = m_all[:n_use]

        p_lo, p_hi, p_upper_1s = bootstrap_ci_pmax_for_keys(
            an,
            q_sub,
            m_sub,
            quantile=quantile,
            B=B,
            alpha=alpha,
            seed=seed,
        )

        print(
            f"n={n_use:3d}"
            + ("" if n_use == n else f" (requested {n})")
            + f" | bootstrap {int((1-alpha)*100)}% CI for p_max: [{p_lo:.4f}, {p_hi:.4f}]"
            + f" | upper 1-sided ({int((1-alpha)*100)}%): {p_upper_1s:.4f}"
        )

        out.append((n_use, p_lo, p_hi, p_upper_1s))

    return out


def plot_bootstrap_ci_vs_n(
    an: DTWAnalyzer,
    ns=(10, 25, 40, 60, 80, 90, 95, 100),
    *,
    quantile=95.0,
    B=2000,
    alpha=0.05,
    seed=0,
    out_path: str | Path | None = None,
):
    """
    Plot CI width (p_hi - p_lo) vs number of flows n.
    """
    import matplotlib.ticker as mticker

    # ---- consistent styling (match histogram panels) ----
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "mathtext.fontset": "dejavusans",
        "svg.fonttype": "path",
    })

    TITLE_SIZE = 40
    AXIS_LABEL_SIZE = 36
    TICK_SIZE = 28

    results = print_bootstrap_ci_vs_n(
        an,
        ns=ns,
        quantile=quantile,
        B=B,
        alpha=alpha,
        seed=seed,
    )
    if not results:
        print("No results to plot.")
        return None

    n_vals = np.array([r[0] for r in results])
    ci_widths = np.array([r[2] - r[1] for r in results])

    fig, ax = plt.subplots(figsize=(9, 7))
    ax.plot(
        n_vals,
        ci_widths,
        marker="o",
        linewidth=3,
        markersize=10,
        color="black",
    )

    ax.set_xlabel("Number of Flows (n)", fontsize=AXIS_LABEL_SIZE, labelpad=12)
    ax.set_ylabel("CI Width", fontsize=AXIS_LABEL_SIZE, labelpad=12)
    ax.tick_params(axis="x", labelsize=TICK_SIZE)
    ax.tick_params(axis="y", labelsize=TICK_SIZE)

    ax.grid(True, alpha=0.2, linewidth=0.5)
    ax.set_title(rf"Bootstrap {(1-alpha)*100:.0f}% CI Width vs Sample Size", fontsize=TITLE_SIZE, pad=15)

    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, pos: f"{x:.3f}"))

    fig.subplots_adjust(left=0.20, right=0.98, bottom=0.22, top=0.90)

    if out_path is None:
        return fig

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="svg")
    plt.close(fig)
    return None


def plot_validation_hist(
    an: DTWAnalyzer,
    *,
    include_x_axis_label: bool = False,
    include_y_axis_label: bool = False,
    x_label: str = "Normalized DTW Distance",
    ci_alpha: float = 0.05,
):
    qvals = np.asarray(an.extract_values(an.qdisc_internal), float)
    mvals = np.asarray(an.extract_values(an.mahi_internal), float)
    cvals = np.asarray(an.extract_values(an.cross_results), float)

    qfinite = qvals[np.isfinite(qvals)]
    mfinite = mvals[np.isfinite(mvals)]
    cfinite = cvals[np.isfinite(cvals)]

    if cfinite.size == 0:
        raise RuntimeError("No cross values for validation histogram.")

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

    return _render_validation_histogram(
        cfinite,
        eps_max,
        p_max,
        x_label=x_label,
        include_x_axis_label=include_x_axis_label,
        include_y_axis_label=include_y_axis_label,
    )