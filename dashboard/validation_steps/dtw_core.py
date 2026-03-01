# validation_steps/dtw_core.py
from __future__ import annotations

from numba import njit
import numpy as np

# ============================================================
# Global Cutoffs (applied before DTW compare)
# ============================================================
CUTOFF_FRONT_MS = 0
CUTOFF_BACK_MS = 0  # set >0 to cut tail window


# ============================================================
# DTW (normalized by warping path length)
# ============================================================
@njit
def _dtw_distance_norm_numba(a, b):
    n = a.shape[0]
    m = b.shape[0]
    if n == 0 or m == 0:
        return np.inf

    INF = 1e30

    prev_cost = np.empty(m + 1, dtype=np.float64)
    curr_cost = np.empty(m + 1, dtype=np.float64)
    prev_len = np.empty(m + 1, dtype=np.int32)
    curr_len = np.empty(m + 1, dtype=np.int32)

    for j in range(m + 1):
        prev_cost[j] = INF
        curr_cost[j] = INF
        prev_len[j] = 0
        curr_len[j] = 0

    prev_cost[0] = 0.0
    prev_len[0] = 0

    for i in range(1, n + 1):
        curr_cost[0] = INF
        curr_len[0] = 0
        ai = a[i - 1]

        for j in range(1, m + 1):
            diff = ai - b[j - 1]
            if diff < 0:
                diff = -diff
            cost = diff

            c1_cost = prev_cost[j]
            c1_len = prev_len[j]

            c2_cost = curr_cost[j - 1]
            c2_len = curr_len[j - 1]

            c3_cost = prev_cost[j - 1]
            c3_len = prev_len[j - 1]

            best_cost = c1_cost
            best_len = c1_len

            if (c2_cost < best_cost) or (c2_cost == best_cost and c2_len < best_len):
                best_cost = c2_cost
                best_len = c2_len
            if (c3_cost < best_cost) or (c3_cost == best_cost and c3_len < best_len):
                best_cost = c3_cost
                best_len = c3_len

            curr_cost[j] = cost + best_cost
            curr_len[j] = best_len + 1

        tmp = prev_cost
        prev_cost = curr_cost
        curr_cost = tmp

        tmpL = prev_len
        prev_len = curr_len
        curr_len = tmpL

    total = prev_cost[m]
    L = prev_len[m]
    if L <= 0:
        return np.inf
    return total / L


def dtw_distance_norm(a, b) -> float:
    a_np = np.asarray(a, dtype=np.float64)
    b_np = np.asarray(b, dtype=np.float64)
    return float(_dtw_distance_norm_numba(a_np, b_np))


# ============================================================
# Mode + time-series helpers
# ============================================================
def is_cumulative_mode(mode: str) -> bool:
    return mode in {
        "ecn_mark",
        "packet_dropped_total",
        "packet_dropped_l4s",
        "packet_dropped_classic",
    }


def series_from_trace(trace: dict, mode: str):
    if mode == "packets":
        return trace.get("q_pkts", [])
    if mode == "bytes":
        return trace.get("q_bytes", [])
    if mode == "ecn_mark":
        return trace.get("ecn_mark", [])
    if mode == "t_ms":
        return trace.get("t_ms", [])

    # delays in ms for BOTH sources after parsing
    if mode == "qdelay_l_ms":
        return trace.get("qdelay_l_ms", [])
    if mode == "qdelay_c_ms":
        return trace.get("qdelay_c_ms", [])

    if mode == "packet_dropped_total":
        if "drop_total" in trace:
            return trace["drop_total"]

        dl4s = trace.get("drop_l4s", [])
        n = len(dl4s)
        dcl = trace.get("drop_classic", [0] * n)
        dovf = trace.get("drop_overflow", [0] * n)
        dovl = trace.get("drop_overload", [0] * n)
        dnet = trace.get("drop_not_ect", [0] * n)
        return [dl4s[i] + dcl[i] + dovf[i] + dovl[i] + dnet[i] for i in range(n)]

    if mode == "packet_dropped_l4s":
        if "drop_l4s" in trace:
            return trace["drop_l4s"]
        return trace.get("drop_total", [])

    if mode == "packet_dropped_classic":
        if "drop_classic" in trace:
            return trace["drop_classic"]
        return trace.get("drop_total", [])

    raise ValueError(f"Unknown mode={mode}")


def diff_per_ms(t_ms, y):
    if not t_ms or not y or len(t_ms) != len(y) or len(y) < 2:
        return [], []

    out_t = []
    out_y = []

    for i in range(1, len(y)):
        dt = float(t_ms[i] - t_ms[i - 1])
        if dt <= 0:
            continue
        dy = float(y[i] - y[i - 1])
        out_t.append(float(t_ms[i]))
        out_y.append(dy)

    return out_t, out_y


def time_and_series_for_compare(trace: dict, mode: str):
    t = trace.get("t_ms", [])
    y = series_from_trace(trace, mode)

    if is_cumulative_mode(mode):
        t, y = diff_per_ms(t, y)

    return t, y


def cutoff_series(
    trace: dict,
    mode: str,
    cutoff_front_ms: int = 0,
    cutoff_back_ms: int = 0,
):
    t, y = time_and_series_for_compare(trace, mode)
    if not t or not y:
        return y

    start = 0
    while start < len(t) and t[start] < cutoff_front_ms:
        start += 1

    end = len(t)
    if cutoff_back_ms > 0:
        t_max = t[-1]
        cutoff_time = t_max - cutoff_back_ms
        while end > start and t[end - 1] >= cutoff_time:
            end -= 1

    return y[start:end]