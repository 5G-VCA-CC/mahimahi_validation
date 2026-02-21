# validation_steps/parser.py
from __future__ import annotations

from pathlib import Path
import re
from multiprocessing import Pool, cpu_count

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({
    "svg.fonttype": "none",
    "font.size": 11,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
})

from tqdm import tqdm
import numpy as np
from numba import njit

CUTOFF_FRONT_MS = 0
CUTOFF_BACK_MS  = 0  # or whatever you want


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
    a_np = np.asarray(a, dtype=np.float64)
    b_np = np.asarray(b, dtype=np.float64)
    return float(_dtw_distance_norm_numba(a_np, b_np))


# ============================================================
# Mode helpers
# ============================================================
def _is_cumulative_mode(mode: str) -> bool:
    return mode in {
        "ecn_mark",
        "packet_dropped_total",
        "packet_dropped_l4s",
        "packet_dropped_classic",
    }


def _series_from_trace(trace: dict, mode: str):
    if mode == "packets":
        return trace.get("q_pkts", [])
    if mode == "bytes":
        return trace.get("q_bytes", [])
    if mode == "ecn_mark":
        return trace.get("ecn_mark", [])
    if mode == "t_ms":
        return trace.get("t_ms", [])

    # NEW: delays (already in ms for BOTH sources after parsing)
    if mode == "qdelay_l_ms":
        return trace.get("qdelay_l_ms", [])
    if mode == "qdelay_c_ms":
        return trace.get("qdelay_c_ms", [])

    if mode == "packet_dropped_total":
        if "drop_total" in trace:
            return trace["drop_total"]

        dl4s = trace.get("drop_l4s", [])
        n = len(dl4s)
        dcl  = trace.get("drop_classic", [0] * n)
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


def _diff_per_ms(t_ms, y):
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


def _time_and_series_for_compare(trace: dict, mode: str):
    t = trace.get("t_ms", [])
    y = _series_from_trace(trace, mode)

    if _is_cumulative_mode(mode):
        t, y = _diff_per_ms(t, y)

    return t, y


def _cutoff_series(trace: dict, mode: str,
                   cutoff_front_ms: int = 0,
                   cutoff_back_ms: int = 0):
    t, y = _time_and_series_for_compare(trace, mode)
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


# ============================================================
# Worker-side parsers
# ============================================================
_TICK_NS_RE = re.compile(r"^(?:TICK_NS|TS_NS)\s+(\d+)\s*$")

_DELAY_RE = re.compile(
    r"\bdelay_c\s+(\d+)([a-zA-Z]+)\s+delay_l\s+(\d+)([a-zA-Z]+)\b"
)

_G_MODE = None


def _pool_init(mode: str):
    global _G_MODE
    _G_MODE = mode


def _parse_int_prefix(s: str, default: int = 0) -> int:
    s = s.strip()
    if not s:
        return default

    i = 0
    sign = 1
    if s[0] == "-":
        sign = -1
        i = 1

    num = 0
    start = i
    while i < len(s) and s[i].isdigit():
        num = num * 10 + (ord(s[i]) - 48)
        i += 1

    if i == start:
        return default
    return sign * num


def _read_mahi_trace_full_worker(path_str: str) -> dict:
    t_ms = []
    q_pkts = []
    q_bytes = []
    ecn_mark = []

    # NEW: delays (already in ms in the log)
    qdelay_l_ms = []
    qdelay_c_ms = []

    # New/primary: single drop counter from "drop="
    drop_total = []

    # Old/optional fields
    drop_l4s = []
    drop_classic = []
    drop_overflow = []
    drop_overload = []
    drop_not_ect = []

    with open(path_str, "r", errors="ignore") as f:
        for line in f:
            if not line.startswith("[QUEUE_STATS]"):
                continue

            parts = line.split()

            tm = None
            qp = qb = em = None

            qdl = None
            qdc = None

            dtot = None
            dl4s = dcl = dovf = dovl = dnet = 0

            for tok in parts:
                if tok.startswith("t_ms="):
                    v = tok[5:].strip().rstrip(",")
                    try:
                        tm = float(v)
                    except Exception:
                        tm = None

                elif tok.startswith("q_pkts="):
                    qp = _parse_int_prefix(tok[7:], default=None)
                elif tok.startswith("q_bytes="):
                    qb = _parse_int_prefix(tok[8:], default=None)
                elif tok.startswith("ecn_mark="):
                    em = _parse_int_prefix(tok[9:], default=None)

                # NEW delays
                elif tok.startswith("qdelay_l_ms="):
                    v = tok[len("qdelay_l_ms="):].strip().rstrip(",")
                    try:
                        qdl = float(v)
                    except Exception:
                        qdl = None
                elif tok.startswith("qdelay_c_ms="):
                    v = tok[len("qdelay_c_ms="):].strip().rstrip(",")
                    try:
                        qdc = float(v)
                    except Exception:
                        qdc = None

                # NEW drop format
                elif tok.startswith("drop="):
                    dtot = _parse_int_prefix(tok[5:], default=0)

                # OLD formats (optional)
                elif tok.startswith("drop_l4s="):
                    dl4s = _parse_int_prefix(tok[9:], default=0)
                elif tok.startswith("drop_classic="):
                    dcl = _parse_int_prefix(tok[13:], default=0)
                elif tok.startswith("drop_overflow="):
                    dovf = _parse_int_prefix(tok[14:], default=0)
                elif tok.startswith("drop_overload="):
                    dovl = _parse_int_prefix(tok[14:], default=0)
                elif tok.startswith("drop_not_ect="):
                    dnet = _parse_int_prefix(tok[13:], default=0)

            if tm is None or qp is None or qb is None or em is None:
                continue

            t_ms.append(tm)
            q_pkts.append(qp)
            q_bytes.append(qb)
            ecn_mark.append(em)

            qdelay_l_ms.append(0.0 if qdl is None else float(qdl))
            qdelay_c_ms.append(0.0 if qdc is None else float(qdc))

            if dtot is None:
                dtot = dl4s + dcl + dovf + dovl + dnet
            drop_total.append(int(dtot))

            drop_l4s.append(dl4s)
            drop_classic.append(dcl)
            drop_overflow.append(dovf)
            drop_overload.append(dovl)
            drop_not_ect.append(dnet)

    return {
        "t_ms": t_ms,
        "q_pkts": q_pkts,
        "q_bytes": q_bytes,
        "ecn_mark": ecn_mark,

        "qdelay_l_ms": qdelay_l_ms,
        "qdelay_c_ms": qdelay_c_ms,

        "drop_total": drop_total,

        "drop_l4s": drop_l4s,
        "drop_classic": drop_classic,
        "drop_overflow": drop_overflow,
        "drop_overload": drop_overload,
        "drop_not_ect": drop_not_ect,
    }


def _parse_tc_num(s: str) -> int:
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

    return int(float(s) * mult)


def _unit_to_ms(v: int, unit: str) -> float:
    u = unit.strip().lower()
    if u in ("us", "usec", "usecs"):
        return v / 1000.0
    if u in ("ms", "msec", "msecs"):
        return float(v)
    if u in ("ns", "nsec", "nsecs"):
        return v / 1e6
    # unknown unit: treat as microseconds
    return v / 1000.0


def _read_qdisc_trace_full_worker(path_str: str) -> dict:
    tr = {
        "t_ms": [], "q_pkts": [], "q_bytes": [], "ecn_mark": [], "drop_total": [],
        "qdelay_c_ms": [], "qdelay_l_ms": [],
    }

    cur_ts = None
    t0 = None

    last_bytes = None
    last_pkts = None
    block_ecn = 0
    block_drop_total = None
    block_qdelay_c_ms = None
    block_qdelay_l_ms = None

    def flush():
        nonlocal cur_ts, t0, last_bytes, last_pkts
        nonlocal block_ecn, block_drop_total, block_qdelay_c_ms, block_qdelay_l_ms

        if cur_ts is None or last_pkts is None or last_bytes is None:
            return
        if t0 is None:
            t0 = cur_ts

        tr["t_ms"].append((cur_ts - t0) / 1e6)
        tr["q_pkts"].append(last_pkts)
        tr["q_bytes"].append(last_bytes)
        tr["ecn_mark"].append(block_ecn)
        tr["drop_total"].append(0 if block_drop_total is None else block_drop_total)
        tr["qdelay_c_ms"].append(0.0 if block_qdelay_c_ms is None else float(block_qdelay_c_ms))
        tr["qdelay_l_ms"].append(0.0 if block_qdelay_l_ms is None else float(block_qdelay_l_ms))

    with open(path_str, "r", errors="ignore") as f:
        for line in f:
            m = _TICK_NS_RE.match(line)
            if m:
                flush()
                cur_ts = int(m.group(1))
                last_bytes = None
                last_pkts = None
                block_ecn = 0
                block_drop_total = None
                block_qdelay_c_ms = None
                block_qdelay_l_ms = None
                continue

            if "backlog " in line:
                parts = line.split()
                for k, tok in enumerate(parts):
                    if tok == "backlog" and k + 2 < len(parts):
                        b = parts[k + 1]
                        p = parts[k + 2]
                        if b.endswith("b") and p.endswith("p"):
                            b = b[:-1]
                            p = p[:-1]
                            last_bytes = _parse_tc_num(b)
                            last_pkts  = _parse_tc_num(p)
                        break
                continue

            if "ecn_mark" in line:
                parts = line.split()
                for k, tok in enumerate(parts):
                    if tok == "ecn_mark" and k + 1 < len(parts):
                        block_ecn = int(parts[k + 1])
                        break

            # NEW: delay_c/delay_l line (usually "prob ... delay_c 0us delay_l 0us")
            if "delay_c" in line and "delay_l" in line:
                mm = _DELAY_RE.search(line)
                if mm:
                    c_val = int(mm.group(1)); c_unit = mm.group(2)
                    l_val = int(mm.group(3)); l_unit = mm.group(4)
                    block_qdelay_c_ms = _unit_to_ms(c_val, c_unit)
                    block_qdelay_l_ms = _unit_to_ms(l_val, l_unit)

            if "dropped" in line:
                idx = line.find("dropped")
                if idx != -1:
                    rest = line[idx + len("dropped"):].lstrip()
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

    a = _cutoff_series(q_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)
    b = _cutoff_series(m_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)

    d = _dtw_distance_norm(a, b)
    return (q_key, m_key, d)


def _internal_worker_paths(args):
    a_key, a_path, b_key, b_path = args

    if a_key.endswith("l"):
        a_tr = _read_qdisc_trace_full_worker(a_path)
    else:
        a_tr = _read_mahi_trace_full_worker(a_path)

    if b_key.endswith("l"):
        b_tr = _read_qdisc_trace_full_worker(b_path)
    else:
        b_tr = _read_mahi_trace_full_worker(b_path)

    a = _cutoff_series(a_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)
    b = _cutoff_series(b_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)

    d = _dtw_distance_norm(a, b)
    return (a_key, b_key, d)


# ============================================================
# DTWAnalyzer
# ============================================================
class DTWAnalyzer:
    _ID_AT_END = re.compile(r"_(\d+)$")

    def __init__(self, qdisc_dir, mahi_dir, mode="packets"):
        self.qdisc_dir = Path(qdisc_dir)
        self.mahi_dir = Path(mahi_dir)
        self.mode = mode  # packets | bytes | ecn_mark | t_ms | packet_dropped_* | qdelay_*_ms

        self.cache_file = Path(f"./dtw_cache_{self.mode}.txt")
        self.num_processes = max(1, cpu_count())

        self.cross_results = []
        self.qdisc_internal = []
        self.mahi_internal = []

        self.cross_dist = {}
        self.qdisc_dist = {}
        self.mahi_dist = {}

        self._qdisc_trace_cache = {}
        self._mahi_trace_cache = {}

    def _get_qdisc_files(self):
        out = []
        for p in sorted(self.qdisc_dir.iterdir()):
            if not p.is_file():
                continue
            if self._ID_AT_END.search(p.stem):
                out.append(p)
        return out

    def _get_mahi_files(self):
        out = []
        for p in sorted(self.mahi_dir.iterdir()):
            if not p.is_file():
                continue
            if self._ID_AT_END.search(p.stem):
                out.append(p)
        return out

    def _extract_id(self, path: Path) -> int:
        m = self._ID_AT_END.search(path.stem)
        if not m:
            raise ValueError(f"Expected filename to end with _<number>: {path.name}")
        return int(m.group(1))

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
        total = n * (n - 1) // 2

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
        total = n * (n - 1) // 2

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

    @staticmethod
    def _extract_values(pairs):
        return [d for (_, _, d) in pairs]

    def plot_triple_hist(self):
        qvals = np.asarray(self._extract_values(self.qdisc_internal), dtype=float)
        mvals = np.asarray(self._extract_values(self.mahi_internal), dtype=float)
        cvals = np.asarray(self._extract_values(self.cross_results), dtype=float)

        q95 = float(np.percentile(qvals[np.isfinite(qvals)], 95)) if qvals.size else np.nan
        m95 = float(np.percentile(mvals[np.isfinite(mvals)], 95)) if mvals.size else np.nan

        eps_min = min(q95, m95)
        eps_max = max(q95, m95)

        cfinite = cvals[np.isfinite(cvals)]
        n = int(cfinite.size)
        if n == 0:
            p_min = np.nan
            p_max = np.nan
        else:
            p_min = float((np.sum(cfinite > eps_min) + 1) / (n + 1))
            p_max = float((np.sum(cfinite > eps_max) + 1) / (n + 1))

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        axes[0].hist(qvals[np.isfinite(qvals)], bins=30, edgecolor="black")
        axes[0].axvline(q95, linewidth=1.5, linestyle="--")
        axes[0].set_title(f"Qdisc DTW Histogram ({self.mode})\nq95={q95:.4g}")

        axes[1].hist(mvals[np.isfinite(mvals)], bins=30, edgecolor="black")
        axes[1].axvline(m95, linewidth=1.5, linestyle="--")
        axes[1].set_title(f"Mahimahi DTW Histogram ({self.mode})\nm95={m95:.4g}")

        axes[2].hist(cfinite, bins=30, edgecolor="black")
        axes[2].axvline(eps_min, linewidth=1.5, linestyle="--")
        axes[2].axvline(eps_max, linewidth=1.5, linestyle="--")
        axes[2].set_title(
            f"Cross-Mode DTW Histogram ({self.mode})\n"
            f"eps_min={eps_min:.4g}, p_min={p_min:.4g} | "
            f"eps_max={eps_max:.4g}, p_max={p_max:.4g}"
        )

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

    def plot_overlay_queue_traces(self, cutoff_ms: int = 0):
        mahi_files = self._get_mahi_files()
        qdisc_files = self._get_qdisc_files()

        fig, axes = plt.subplots(1, 2, figsize=(18, 5))

        all_y_plotted = []
        global_xmax = 0.0

        ax = axes[0]
        for f in mahi_files:
            tr = self.read_mahi_trace_full(f)
            t_ms, y = _time_and_series_for_compare(tr, self.mode)
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

        ax.set_title(f"Mahimahi {self.mode} vs time (overlay)")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(self.mode)
        ax.grid(True, alpha=0.2)

        ax = axes[1]
        for f in qdisc_files:
            tr = self.read_qdisc_trace_full(f)
            t_ms, y = _time_and_series_for_compare(tr, self.mode)
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

        ax.set_title(f"Linux qdisc {self.mode} vs time (overlay)")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(self.mode)
        ax.grid(True, alpha=0.2)

        ylim = self._robust_ylim(all_y_plotted, lo=1, hi=99, pad_frac=0.05)
        if ylim is not None:
            for ax in axes:
                ax.set_ylim(*ylim)
                ax.set_xlim(0, global_xmax)

        plt.tight_layout()
        return fig