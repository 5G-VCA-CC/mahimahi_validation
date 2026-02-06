# parser.py
from __future__ import annotations

from pathlib import Path
import re
from multiprocessing import Pool, cpu_count
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from tqdm import tqdm
from dtaidistance import dtw

CUTOFF_MS = 3000  # drop samples with t_ms < CUTOFF_MS

# ============================================================
# DTW + series helpers (pure functions)
# ============================================================
# def _dtw_distance_norm(a, b) -> float:
#     """
#     DTW distance normalized by the length of the optimal warping path.
#     This yields a mean per-alignment-step deviation.

#     Returns:
#         total_cost / path_len
#     """
#     n, m = len(a), len(b)
#     if n == 0 or m == 0:
#         return float("inf")

#     INF = float("inf")

#     # dp_cost[j] = min cost to align a[:i] with b[:j] for current i
#     prev_cost = [INF] * (m + 1)
#     curr_cost = [INF] * (m + 1)

#     # dp_len[j] = warping path length for the argmin alignment
#     prev_len = [0] * (m + 1)
#     curr_len = [0] * (m + 1)

#     prev_cost[0] = 0.0
#     prev_len[0] = 0

#     for i in range(1, n + 1):
#         curr_cost[0] = INF
#         curr_len[0] = 0

#         ai = a[i - 1]
#         for j in range(1, m + 1):
#             cost = abs(ai - b[j - 1])

#             # candidates: (total_cost, path_len)
#             c1_cost, c1_len = prev_cost[j], prev_len[j]       # (i-1, j)
#             c2_cost, c2_len = curr_cost[j - 1], curr_len[j - 1] # (i, j-1)
#             c3_cost, c3_len = prev_cost[j - 1], prev_len[j - 1] # (i-1, j-1)

#             # pick best predecessor by cost (tie-break: shorter path)
#             best_cost, best_len = c1_cost, c1_len
#             if (c2_cost < best_cost) or (c2_cost == best_cost and c2_len < best_len):
#                 best_cost, best_len = c2_cost, c2_len
#             if (c3_cost < best_cost) or (c3_cost == best_cost and c3_len < best_len):
#                 best_cost, best_len = c3_cost, c3_len

#             curr_cost[j] = cost + best_cost
#             curr_len[j] = best_len + 1

#         prev_cost, curr_cost = curr_cost, prev_cost
#         prev_len, curr_len = curr_len, prev_len

#     total = prev_cost[m]
#     path_len = prev_len[m]
#     if path_len <= 0:
#         return float("inf")

import numpy as np
from numba import njit

def _bucket_mean(t_ms, y, window_ms: float):
    """
    Bucket an irregular/regular time series into fixed windows [0, window_ms), [window_ms, 2*window_ms), ...
    Returns (t_bucket, y_bucket) where:
      - t_bucket is the center time of the window
      - y_bucket is the mean(y) of samples that landed in that window
    Assumes t_ms is non-decreasing.
    """
    if not t_ms or not y or len(t_ms) != len(y):
        return [], []

    out_t = []
    out_y = []

    w = float(window_ms)
    if w <= 0:
        return list(t_ms), list(y)

    # window index based on time since start (t_ms[0] should be ~0 for your traces)
    cur_bin = int(t_ms[0] // w)
    acc = 0.0
    cnt = 0

    def flush(bin_idx, acc, cnt):
        if cnt <= 0:
            return
        start = bin_idx * w
        center = start + 0.5 * w
        out_t.append(center)
        out_y.append(acc / cnt)

    for ti, yi in zip(t_ms, y):
        bin_idx = int(ti // w)
        if bin_idx != cur_bin:
            flush(cur_bin, acc, cnt)
            cur_bin = bin_idx
            acc = 0.0
            cnt = 0
        acc += float(yi)
        cnt += 1

    flush(cur_bin, acc, cnt)
    return out_t, out_y

@njit
def _dtw_distance_norm_numba(a, b):
    """
    DTW distance normalized by optimal warping path length.
    Same as your Python version: tie-break by shorter path on equal cost.
    """
    n = a.shape[0]
    m = b.shape[0]
    if n == 0 or m == 0:
        return np.inf

    INF = 1e30  # big finite number (faster than np.inf inside tight loops)

    prev_cost = np.empty(m + 1, dtype=np.float64)
    curr_cost = np.empty(m + 1, dtype=np.float64)
    prev_len  = np.empty(m + 1, dtype=np.int32)
    curr_len  = np.empty(m + 1, dtype=np.int32)

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
            c1_len  = prev_len[j]

            c2_cost = curr_cost[j - 1]
            c2_len  = curr_len[j - 1]

            c3_cost = prev_cost[j - 1]
            c3_len  = prev_len[j - 1]

            best_cost = c1_cost
            best_len  = c1_len

            if (c2_cost < best_cost) or (c2_cost == best_cost and c2_len < best_len):
                best_cost = c2_cost
                best_len  = c2_len
            if (c3_cost < best_cost) or (c3_cost == best_cost and c3_len < best_len):
                best_cost = c3_cost
                best_len  = c3_len

            curr_cost[j] = cost + best_cost
            curr_len[j]  = best_len + 1

        # swap buffers
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


def _dtw_distance_norm(a, b):
    # Convert once per call (lists -> numpy arrays)
    # If you can, pass np arrays directly from upstream to avoid this conversion cost.
    a_np = np.asarray(a, dtype=np.float64)
    b_np = np.asarray(b, dtype=np.float64)
    return float(_dtw_distance_norm_numba(a_np, b_np))

# >>> CHANGED: cumulative modes will be converted to rate (delta per ms), so baseline_zero not used
def _is_cumulative_mode(mode: str) -> bool:
    return mode in {
        "ecn_mark",
        "packet_dropped_total",
        "packet_dropped_l4s",
        "packet_dropped_classic",
    }
# <<< CHANGED


def _series_from_trace(trace: dict, mode: str):
    if mode == "packets":
        return trace["q_pkts"]
    if mode == "bytes":
        return trace["q_bytes"]
    if mode == "ecn_mark":
        return trace["ecn_mark"]
    if mode == "t_ms":
        return trace["t_ms"]

    if mode == "packet_dropped_total":
        if "drop_total" in trace:  # qdisc
            return trace["drop_total"]
        # mahi: sum (cumulative)
        return [
            trace["drop_l4s"][i] + trace["drop_classic"][i] + trace["drop_overload"][i]
            for i in range(len(trace["drop_l4s"]))
        ]

    if mode == "packet_dropped_l4s":
        if "drop_l4s" in trace:  # mahi
            return trace["drop_l4s"]
        return trace["drop_total"]  # qdisc fallback

    if mode == "packet_dropped_classic":
        if "drop_classic" in trace:  # mahi
            return trace["drop_classic"]
        return trace["drop_total"]  # qdisc fallback

    raise ValueError(f"Unknown mode={mode}")


def _diff_per_ms(t_ms, y):
    """
    Convert cumulative y(t) into rate: delta(y)/delta(t) per ms.
    Returns (t_aligned, rate), where t_aligned == t_ms[1:].
    """
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
        out_y.append(dy / dt)

    return out_t, out_y

def _time_and_series_for_compare(trace: dict, mode: str):
    """
    Returns (t_ms_aligned, y_aligned) ready for DTW / plotting.

    - Non-cumulative modes:
        return original t_ms and y
    - Cumulative modes:
        convert to rate via _diff_per_ms
    - Additionally, for Mahimahi traces only:
        bucket into MAHI_BUCKET_MS windows to match Linux 'tc' observation granularity
    """
    t = trace["t_ms"]
    y = _series_from_trace(trace, mode)

    if _is_cumulative_mode(mode):
        t, y = _diff_per_ms(t, y)

    # Mahimahi traces have these keys; qdisc traces do not.
    return t, y

# <<< CHANGED


# >>> CHANGED: cutoff now uses time+series aligned (rate series for cumulative modes)
def _cutoff_series(trace: dict, mode: str, cutoff_ms: int):
    t, y = _time_and_series_for_compare(trace, mode)
    if not t or not y:
        return y

    k = 0
    while k < len(t) and t[k] < cutoff_ms:
        k += 1
    return y[k:]
# <<< CHANGED


# ============================================================
# Worker-side parsers (top-level, picklable)
#   IMPORTANT: workers read files -> we do NOT pickle trace dicts
# ============================================================
# qdisc block header
_TICK_NS_RE = re.compile(r"^(?:TICK_NS|TS_NS)\s+(\d+)\s*$")
TS_NS_RE = _TICK_NS_RE
_BACKLOG_RE = re.compile(r"\bbacklog\s+(\d+)b\s+(\d+)p\b")
_ECN_RE = re.compile(r"\becn_mark\s+(\d+)\b")
_DROPPED_RE = re.compile(r"\(\s*dropped\s+(\d+)\s*,")

# Mahimahi QUEUE_STATS
_QUEUE_STATS_RE = re.compile(
    r"^\[QUEUE_STATS\]\s+t_ms=([0-9.eE+-]+)\s+q_pkts=(\d+)\s+q_bytes=(\d+)\s+ecn_mark=(\d+)"
    r"(?:\s+drop_l4s=(\d+)\s+drop_classic=(\d+)\s+drop_overload=(\d+))?"
)

_G_MODE = None


def _pool_init(mode: str):
    global _G_MODE
    _G_MODE = mode


def _read_mahi_trace_full_worker(path_str: str) -> dict:
    q_pkts = []
    q_bytes = []
    ecn_mark = []
    drop_l4s = []
    drop_classic = []
    drop_overload = []

    with open(path_str, "r", errors="ignore") as f:
        for line in f:
            if not line.startswith("[QUEUE_STATS]"):
                continue

            # Example tokens:
            # [QUEUE_STATS] t_ms=... q_pkts=... q_bytes=... ecn_mark=... drop_l4s=... drop_classic=... drop_overload=...
            parts = line.split()

            # defaults if drops missing
            dl4s = 0
            dcl  = 0
            dov  = 0

            qp = qb = em = None

            for tok in parts:
                if tok.startswith("q_pkts="):
                    qp = int(tok[7:])
                elif tok.startswith("q_bytes="):
                    qb = int(tok[8:])
                elif tok.startswith("ecn_mark="):
                    em = int(tok[9:])
                elif tok.startswith("drop_l4s="):
                    dl4s = int(tok[9:])
                elif tok.startswith("drop_classic="):
                    dcl = int(tok[13:])
                elif tok.startswith("drop_overload="):
                    dov = int(tok[14:])

            if qp is None or qb is None or em is None:
                continue

            q_pkts.append(qp)
            q_bytes.append(qb)
            ecn_mark.append(em)
            drop_l4s.append(dl4s)
            drop_classic.append(dcl)
            drop_overload.append(dov)

    # synthetic uniform time grid: 0, 16, 32, ...
    n = len(q_pkts)
    t_ms = [i * 16.0 for i in range(n)]

    return {
        "t_ms": t_ms,
        "q_pkts": q_pkts,
        "q_bytes": q_bytes,
        "ecn_mark": ecn_mark,
        "drop_l4s": drop_l4s,
        "drop_classic": drop_classic,
        "drop_overload": drop_overload,
    }

def _parse_tc_num(s: str) -> int:
    """
    Parse tc-style numbers like:
      '74', '74K', '1.5M', '2G'
    Returns an integer scaled by 1000^k (tc commonly uses decimal units).
    """
    s = s.strip()
    if not s:
        return 0

    mult = 1
    last = s[-1]
    if last in ("K", "M", "G", "T"):
        if last == "K":
            mult = 1_000
        elif last == "M":
            mult = 1_000_000
        elif last == "G":
            mult = 1_000_000_000
        elif last == "T":
            mult = 1_000_000_000_000
        s = s[:-1]

    # allow decimals like "1.5M"
    return int(float(s) * mult)

_TICK_NS_RE = re.compile(r"^(?:TICK_NS|TS_NS)\s+(\d+)\s*$")

def _read_qdisc_trace_full_worker(path_str: str) -> dict:
    tr = {"t_ms": [], "q_pkts": [], "q_bytes": [], "ecn_mark": [], "drop_total": []}

    cur_ts = None
    t0 = None

    last_bytes = None
    last_pkts = None
    block_ecn = 0
    block_drop_total = None

    def flush():
        nonlocal cur_ts, t0, last_bytes, last_pkts, block_ecn, block_drop_total
        if cur_ts is None or last_pkts is None or last_bytes is None:
            return
        if t0 is None:
            t0 = cur_ts
        tr["t_ms"].append((cur_ts - t0) / 1e6)
        tr["q_pkts"].append(last_pkts)
        tr["q_bytes"].append(last_bytes)
        tr["ecn_mark"].append(block_ecn)
        tr["drop_total"].append(0 if block_drop_total is None else block_drop_total)

    with open(path_str, "r", errors="ignore") as f:
        for line in f:
            m = _TICK_NS_RE.match(line)
            if m:
                flush()
                cur_ts = int(m.group(1))  # works for TICK_NS or TS_NS
                last_bytes = None
                last_pkts = None
                block_ecn = 0
                block_drop_total = None
                continue

            # backlog <bytes>b <pkts>p
            # cheap check before parsing
            if "backlog " in line:
                parts = line.split()
                # find "backlog" token and read next two tokens
                # (robust enough for tc -s style lines)
                for k, tok in enumerate(parts):
                    if tok == "backlog" and k + 2 < len(parts):
                        b = parts[k + 1]
                        p = parts[k + 2]
                        if b.endswith("b") and p.endswith("p"):
                            b = b[:-1]   # drop trailing 'b'
                            p = p[:-1]   # drop trailing 'p'
                            last_bytes = _parse_tc_num(b)
                            last_pkts  = _parse_tc_num(p)  # pkts usually plain int, but safe
                        break
                continue

            if "ecn_mark" in line:
                parts = line.split()
                for k, tok in enumerate(parts):
                    if tok == "ecn_mark" and k + 1 < len(parts):
                        block_ecn = int(parts[k + 1])
                        break

            if "dropped" in line:
                # look for "(dropped N," pattern without regex
                idx = line.find("dropped")
                if idx != -1:
                    rest = line[idx + len("dropped"):].lstrip()
                    # parse leading integer
                    num = 0
                    sign = 1
                    i = 0
                    if i < len(rest) and rest[i] == "-":
                        sign = -1
                        i += 1
                    while i < len(rest) and rest[i].isdigit():
                        num = num * 10 + (ord(rest[i]) - 48)
                        i += 1
                    block_drop_total = sign * num

    flush()
    return tr


def _cross_worker_paths(args):
    q_key, q_path, m_key, m_path = args
    q_tr = _read_qdisc_trace_full_worker(q_path)
    m_tr = _read_mahi_trace_full_worker(m_path)

    a = _cutoff_series(q_tr, _G_MODE, CUTOFF_MS)
    b = _cutoff_series(m_tr, _G_MODE, CUTOFF_MS)
    d = _dtw_distance_norm(a, b)
    return (q_key, m_key, d)


def _internal_worker_paths(args):
    a_key, a_path, b_key, b_path = args

    # decide parser by suffix: 'l' (linux/qdisc) vs 'm' (mahimahi)
    if a_key.endswith("l"):
        a_tr = _read_qdisc_trace_full_worker(a_path)
    else:
        a_tr = _read_mahi_trace_full_worker(a_path)

    if b_key.endswith("l"):
        b_tr = _read_qdisc_trace_full_worker(b_path)
    else:
        b_tr = _read_mahi_trace_full_worker(b_path)

    a = _cutoff_series(a_tr, _G_MODE, CUTOFF_MS)
    b = _cutoff_series(b_tr, _G_MODE, CUTOFF_MS)
    d = _dtw_distance_norm(a, b)
    return (a_key, b_key, d)


# ============================================================
# DTWAnalyzer (main process)
#   - still keeps a parse cache for plotting
#   - BUT compute_* does not ship traces to workers anymore
# ============================================================
class DTWAnalyzer:
    _ID_AT_END = re.compile(r"_(\d+)$")

    # (kept for main-process parsing cache / plotting)
    BACKLOG_RE = _BACKLOG_RE
    ECN_RE = _ECN_RE
    DROPPED_RE = _DROPPED_RE
    QUEUE_STATS_RE = _QUEUE_STATS_RE

    def __init__(self, qdisc_dir, mahi_dir, mode="packets"):
        self.qdisc_dir = Path(qdisc_dir)
        self.mahi_dir = Path(mahi_dir)
        self.mode = mode  # packets | bytes | ecn_mark | t_ms | packet_dropped_total | packet_dropped_l4s | packet_dropped_classic

        self.cache_file = Path(f"./dtw_cache_{self.mode}.txt")
        self.num_processes = max(1, cpu_count())

        self.cross_results = []
        self.qdisc_internal = []
        self.mahi_internal = []

        self.cross_dist = {}
        self.qdisc_dist = {}
        self.mahi_dist = {}

        # parsed traces for plotting only (main process)
        self._qdisc_trace_cache = {}  # str(path) -> trace dict
        self._mahi_trace_cache = {}   # str(path) -> trace dict

    # ----------------------------
    # file lists
    # ----------------------------
    def _get_qdisc_files(self):
        return sorted(p for p in self.qdisc_dir.iterdir() if p.is_file())

    def _get_mahi_files(self):
        return sorted(p for p in self.mahi_dir.iterdir() if p.is_file())

    def _extract_id(self, path: Path) -> int:
        m = self._ID_AT_END.search(path.stem)
        if not m:
            raise ValueError(f"Expected filename to end with _<number>: {path.name}")
        return int(m.group(1))

    # ----------------------------
    # Cache load/save
    # ----------------------------
    def load_cache(self):
        if not self.cache_file.exists():
            return False

        self.cross_results.clear()
        self.qdisc_internal.clear()
        self.mahi_internal.clear()
        self.cross_dist.clear()
        self.qdisc_dist.clear()
        self.mahi_dist.clear()

        with open(self.cache_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                a, b, d = line.split(",")
                d = float(d)

                if a.endswith("l") and b.endswith("m"):
                    self.cross_dist.setdefault(a, {})[b] = d
                    self.cross_results.append((a, b, d))
                elif a.endswith("l") and b.endswith("l"):
                    self.qdisc_dist.setdefault(a, {})[b] = d
                    self.qdisc_dist.setdefault(b, {})[a] = d
                    self.qdisc_internal.append((a, b, d))
                elif a.endswith("m") and b.endswith("m"):
                    self.mahi_dist.setdefault(a, {})[b] = d
                    self.mahi_dist.setdefault(b, {})[a] = d
                    self.mahi_internal.append((a, b, d))

        return True

    def save_cache(self):
        with open(self.cache_file, "w") as f:
            for a, b, d in self.cross_results:
                f.write(f"{a},{b},{d}\n")
            for a, b, d in self.qdisc_internal:
                f.write(f"{a},{b},{d}\n")
            for a, b, d in self.mahi_internal:
                f.write(f"{a},{b},{d}\n")

        print(f"[cache] wrote {self.cache_file.resolve()}")

    # ----------------------------
    # Parsing (main process cache, used for plotting)
    # ----------------------------
    def read_mahi_trace_full(self, path: Path):
        key = str(path)
        if key in self._mahi_trace_cache:
            return self._mahi_trace_cache[key]

        tr = _read_mahi_trace_full_worker(key)
        self._mahi_trace_cache[key] = tr
        return tr

    def read_qdisc_trace_full(self, path: Path):
        key = str(path)
        if key in self._qdisc_trace_cache:
            return self._qdisc_trace_cache[key]

        tr = _read_qdisc_trace_full_worker(key)
        self._qdisc_trace_cache[key] = tr
        return tr

    # ----------------------------
    # DTW compute (NO pickling traces)
    # ----------------------------
    def compute_cross(self):
        q_files = self._get_qdisc_files()
        m_files = self._get_mahi_files()

        q_pairs = [(f"{self._extract_id(qf)}l", str(qf)) for qf in q_files]
        m_pairs = [(f"{self._extract_id(mf)}m", str(mf)) for mf in m_files]

        total = len(q_pairs) * len(m_pairs)

        def job_iter():
            for qk, qp in q_pairs:
                for mk, mp in m_pairs:
                    yield (qk, qp, mk, mp)

        self.cross_results = []
        self.cross_dist = {}

        with Pool(
            processes=self.num_processes,
            initializer=_pool_init,
            initargs=(self.mode,),
        ) as pool:
            for a, b, d in tqdm(
                pool.imap_unordered(_cross_worker_paths, job_iter(), chunksize=32),
                total=total,
                desc="Cross DTW",
                smoothing=0.05,
            ):
                self.cross_results.append((a, b, d))
                self.cross_dist.setdefault(a, {})[b] = d

    def compute_qdisc_internal(self):
        q_files = self._get_qdisc_files()
        items = [(f"{self._extract_id(f)}l", str(f)) for f in q_files]

        n = len(items)
        total = n * (n - 1) // 2  # number of unique pairs

        def job_iter():
            for i in range(len(items)):
                a_key, a_path = items[i]
                for j in range(i + 1, len(items)):
                    b_key, b_path = items[j]
                    yield (a_key, a_path, b_key, b_path)

        self.qdisc_internal = []
        self.qdisc_dist = {}

        with Pool(
            processes=self.num_processes,
            initializer=_pool_init,
            initargs=(self.mode,),
        ) as pool:
            for a, b, d in tqdm(
                pool.imap_unordered(_internal_worker_paths, job_iter(), chunksize=32),
                total=total,
                desc="Qdisc internal DTW",
                smoothing=0.05,
            ):
                self.qdisc_internal.append((a, b, d))
                self.qdisc_dist.setdefault(a, {})[b] = d
                self.qdisc_dist.setdefault(b, {})[a] = d

    def compute_mahi_internal(self):
        m_files = self._get_mahi_files()
        items = [(f"{self._extract_id(f)}m", str(f)) for f in m_files]

        n = len(items)
        total = n * (n - 1) // 2  # number of unique pairs

        def job_iter():
            for i in range(len(items)):
                a_key, a_path = items[i]
                for j in range(i + 1, len(items)):
                    b_key, b_path = items[j]
                    yield (a_key, a_path, b_key, b_path)

        self.mahi_internal = []
        self.mahi_dist = {}

        with Pool(
            processes=self.num_processes,
            initializer=_pool_init,
            initargs=(self.mode,),
        ) as pool:
            for a, b, d in tqdm(
                pool.imap_unordered(_internal_worker_paths, job_iter(), chunksize=32),
                total=total,
                desc="Mahimahi internal DTW",
                smoothing=0.05,
            ):
                self.mahi_internal.append((a, b, d))
                self.mahi_dist.setdefault(a, {})[b] = d
                self.mahi_dist.setdefault(b, {})[a] = d

    # ----------------------------
    # Plotting
    # ----------------------------
    @staticmethod
    def _extract_values(pairs):
        return [d for (_, _, d) in pairs]

    def plot_triple_hist(self):
        qvals = self._extract_values(self.qdisc_internal)
        mvals = self._extract_values(self.mahi_internal)
        cvals = self._extract_values(self.cross_results)

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        axes[0].hist(qvals, bins=30, edgecolor="black")
        axes[0].set_title(f"Qdisc DTW Histogram ({self.mode})")

        axes[1].hist(mvals, bins=30, edgecolor="black")
        axes[1].set_title(f"Mahimahi DTW Histogram ({self.mode})")

        axes[2].hist(cvals, bins=30, edgecolor="black")
        axes[2].set_title(f"Cross-Mode DTW Histogram ({self.mode})")

        plt.tight_layout()
        return fig

    def plot_triple_cdf(self):
        qvals = sorted(self._extract_values(self.qdisc_internal))
        mvals = sorted(self._extract_values(self.mahi_internal))
        cvals = sorted(self._extract_values(self.cross_results))

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        if qvals:
            axes[0].plot(qvals, [i / len(qvals) for i in range(len(qvals))])
        axes[0].set_title(f"Qdisc DTW CDF ({self.mode})")

        if mvals:
            axes[1].plot(mvals, [i / len(mvals) for i in range(len(mvals))])
        axes[1].set_title(f"Mahimahi DTW CDF ({self.mode})")

        if cvals:
            axes[2].plot(cvals, [i / len(cvals) for i in range(len(cvals))])
        axes[2].set_title(f"Cross-Mode DTW CDF ({self.mode})")

        plt.tight_layout()
        return fig

    def plot_overlay_queue_traces(self, cutoff_ms: int = 0):
        mahi_files = self._get_mahi_files()
        qdisc_files = self._get_qdisc_files()

        fig, axes = plt.subplots(1, 2, figsize=(18, 5))

        global_ymin = float("inf")
        global_ymax = float("-inf")
        global_xmax = 0.0

        ax = axes[0]
        for f in mahi_files:
            tr = self.read_mahi_trace_full(f)
            # >>> CHANGED: use aligned time+series (rate series for cumulative modes)
            t_ms, y = _time_and_series_for_compare(tr, self.mode)
            # <<< CHANGED
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
            global_ymin = min(global_ymin, min(y2))
            global_ymax = max(global_ymax, max(y2))
            global_xmax = max(global_xmax, float(t2[-1]))

        ax.set_title(f"Mahimahi {self.mode} vs time (overlay)")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(self.mode)
        ax.grid(True, alpha=0.2)

        ax = axes[1]
        for f in qdisc_files:
            tr = self.read_qdisc_trace_full(f)
            # >>> CHANGED: use aligned time+series (rate series for cumulative modes)
            t_ms, y = _time_and_series_for_compare(tr, self.mode)
            # <<< CHANGED
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
            global_ymin = min(global_ymin, min(y2))
            global_ymax = max(global_ymax, max(y2))
            global_xmax = max(global_xmax, float(t2[-1]))

        ax.set_title(f"Linux qdisc {self.mode} vs time (overlay)")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(self.mode)
        ax.grid(True, alpha=0.2)

        if global_ymin != float("inf") and global_ymax != float("-inf"):
            for ax in axes:
                ax.set_ylim(global_ymin, global_ymax)
                ax.set_xlim(0, global_xmax)

        plt.tight_layout()
        return fig
