from __future__ import annotations

from pathlib import Path
import re
from itertools import product
from multiprocessing import Pool, cpu_count
import matplotlib.pyplot as plt


# ----------------------------
# DTW (CPU) — normalized by max(len)
# ----------------------------
def _dtw_distance_norm(a, b) -> float:
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return float("inf")

    INF = float("inf")
    prev = [INF] * (m + 1)
    curr = [INF] * (m + 1)
    prev[0] = 0.0

    for i in range(1, n + 1):
        curr[0] = INF
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = abs(ai - b[j - 1])
            curr[j] = cost + min(prev[j], curr[j - 1], prev[j - 1])
        prev, curr = curr, prev

    return prev[m] / max(n, m)


# ----------------------------
# Helpers: baseline normalize / delta
# ----------------------------
def _baseline_zero(y):
    """Shift series so y[0] becomes 0 (for cumulative counters)."""
    if not y:
        return y
    y0 = y[0]
    return [v - y0 for v in y]


def _to_deltas(y):
    """Convert cumulative series to per-sample increments (non-negative)."""
    if not y:
        return y
    out = [0]
    for i in range(1, len(y)):
        out.append(y[i] - y[i - 1])
    return out


def _is_cumulative_mode(mode: str) -> bool:
    # These are typically reported as cumulative counters.
    return mode in {
        "ecn_mark",
        "packet_dropped_total",
        "packet_dropped_l4s",
        "packet_dropped_classic",
    }


def _series_from_trace(trace: dict, mode: str):
    if mode == "packets":
        return trace["q_pkts"]
    if mode == "bytes":
        return trace["q_bytes"]
    if mode == "ecn_mark":
        return trace["ecn_mark"]
    if mode == "t_ms":
        return trace["t_ms"]

    # ---- DROPS ----
    if mode == "packet_dropped_total":
        # qdisc: already total (cumulative)
        if "drop_total" in trace:
            return trace["drop_total"]
        # mahi: sum components (include overload as "total drops") (cumulative)
        return [
            trace["drop_l4s"][i] + trace["drop_classic"][i] + trace["drop_overload"][i]
            for i in range(len(trace["drop_l4s"]))
        ]

    if mode == "packet_dropped_l4s":
        if "drop_l4s" in trace:
            return trace["drop_l4s"]
        # qdisc has no per-class breakdown → fall back to total
        return trace["drop_total"]

    if mode == "packet_dropped_classic":
        if "drop_classic" in trace:
            return trace["drop_classic"]
        # qdisc has no per-class breakdown → fall back to total
        return trace["drop_total"]

    raise ValueError(f"Unknown mode={mode}")


def _series_for_compare(trace: dict, mode: str):
    """
    What DTW/plots should operate on.
    For cumulative counters, baseline-normalize so both start at 0.
    """
    y = _series_from_trace(trace, mode)
    if _is_cumulative_mode(mode):
        return _baseline_zero(y)
    return y


# ----------------------------
# multiprocessing workers (must be top-level picklable)
# ----------------------------
def _cross_worker(args):
    a_key, a_trace, b_key, b_trace, mode = args
    a = _series_for_compare(a_trace, mode)
    b = _series_for_compare(b_trace, mode)
    d = _dtw_distance_norm(a, b)
    return (a_key, b_key, d)


def _internal_worker(args):
    a_key, a_trace, b_key, b_trace, mode = args
    a = _series_for_compare(a_trace, mode)
    b = _series_for_compare(b_trace, mode)
    d = _dtw_distance_norm(a, b)
    return (a_key, b_key, d)


class DTWAnalyzer:
    _ID_AT_END = re.compile(r"_(\d+)$")

    # qdisc block header
    TS_NS_RE = re.compile(r"^TS_NS\s+(\d+)\s*$")

    # qdisc fields we need
    BACKLOG_RE = re.compile(r"\bbacklog\s+(\d+)b\s+(\d+)p\b")
    ECN_RE = re.compile(r"\becn_mark\s+(\d+)\b")

    # Mahimahi QUEUE_STATS
    QUEUE_STATS_RE = re.compile(
        r"^\[QUEUE_STATS\]\s+t_ms=([0-9.eE+-]+)\s+q_pkts=(\d+)\s+q_bytes=(\d+)\s+ecn_mark=(\d+)"
        r"(?:\s+drop_l4s=(\d+)\s+drop_classic=(\d+)\s+drop_overload=(\d+))?"
    )

    DROPPED_RE = re.compile(r"\(\s*dropped\s+(\d+)\s*,")

    def __init__(self, qdisc_dir, mahi_dir, mode="packets"):
        self.qdisc_dir = Path(qdisc_dir)
        self.mahi_dir = Path(mahi_dir)
        self.mode = mode  # packets | bytes | ecn_mark | t_ms | packet_dropped_total | packet_dropped_l4s | packet_dropped_classic

        # ✅ one cache file per variable (no args in main)
        self.cache_file = Path(f"./dtw_cache_{self.mode}.txt")

        # all cores
        self.num_processes = max(1, cpu_count())

        # results
        self.cross_results = []
        self.qdisc_internal = []
        self.mahi_internal = []

        self.cross_dist = {}
        self.qdisc_dist = {}
        self.mahi_dist = {}

        # parsed traces
        self._qdisc_trace_cache = {}  # str(path) -> trace dict (NEVER None)
        self._mahi_trace_cache = {}   # str(path) -> trace dict (NEVER None)

        self._qdisc_files = None
        self._mahi_files = None

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

                # cross
                if a.endswith("l") and b.endswith("m"):
                    self.cross_dist.setdefault(a, {})[b] = d
                    self.cross_results.append((a, b, d))

                # qdisc internal
                elif a.endswith("l") and b.endswith("l"):
                    self.qdisc_dist.setdefault(a, {})[b] = d
                    self.qdisc_dist.setdefault(b, {})[a] = d
                    self.qdisc_internal.append((a, b, d))

                # mahi internal
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
    # Parsing
    # ----------------------------
    def read_mahi_trace_full(self, path: Path):
        key = str(path)
        if key in self._mahi_trace_cache:
            return self._mahi_trace_cache[key]

        tr = {
            "t_ms": [],
            "q_pkts": [],
            "q_bytes": [],
            "ecn_mark": [],
            "drop_l4s": [],
            "drop_classic": [],
            "drop_overload": [],
        }

        with open(path, "r", errors="ignore") as f:
            for line in f:
                m = self.QUEUE_STATS_RE.match(line)
                if not m:
                    continue

                tr["q_pkts"].append(int(m.group(2)))
                tr["q_bytes"].append(int(m.group(3)))
                tr["ecn_mark"].append(int(m.group(4)))

                if m.group(5) is None:
                    tr["drop_l4s"].append(0)
                    tr["drop_classic"].append(0)
                    tr["drop_overload"].append(0)
                else:
                    tr["drop_l4s"].append(int(m.group(5)))
                    tr["drop_classic"].append(int(m.group(6)))
                    tr["drop_overload"].append(int(m.group(7)))

        # synthetic uniform time grid: 0ms, 16ms, 32ms, ...
        step = 16.0
        tr["t_ms"] = [i * step for i in range(len(tr["q_pkts"]))]

        self._mahi_trace_cache[key] = tr
        return tr

    def read_qdisc_trace_full(self, path: Path) -> dict:
        key = str(path)
        tr = self._qdisc_trace_cache.get(key)
        if tr is not None:
            return tr

        tr = {"t_ms": [], "q_pkts": [], "q_bytes": [], "ecn_mark": [], "drop_total": []}

        cur_ts = None
        t0 = None

        last_bytes = None
        last_pkts = None
        block_ecn = 0
        block_drop_total = None

        def flush():
            nonlocal cur_ts, t0, last_bytes, last_pkts, block_ecn, block_drop_total
            if cur_ts is None:
                return
            if last_pkts is None or last_bytes is None:
                return
            if t0 is None:
                t0 = cur_ts

            tr["t_ms"].append((cur_ts - t0) / 1e6)  # ns -> ms
            tr["q_pkts"].append(last_pkts)
            tr["q_bytes"].append(last_bytes)
            tr["ecn_mark"].append(block_ecn)
            tr["drop_total"].append(0 if block_drop_total is None else block_drop_total)

        with open(path, "r", errors="ignore") as f:
            for line in f:
                m_ts = self.TS_NS_RE.match(line)
                if m_ts:
                    flush()
                    cur_ts = int(m_ts.group(1))
                    last_bytes = None
                    last_pkts = None
                    block_ecn = 0
                    block_drop_total = None
                    continue

                m_bl = self.BACKLOG_RE.search(line)
                if m_bl:
                    last_bytes = int(m_bl.group(1))
                    last_pkts = int(m_bl.group(2))
                    continue

                m_ecn = self.ECN_RE.search(line)
                if m_ecn:
                    block_ecn = int(m_ecn.group(1))

                m_dr = self.DROPPED_RE.search(line)
                if m_dr:
                    # keeps the last "dropped X" seen in the TS block (often dualpi2)
                    block_drop_total = int(m_dr.group(1))

        flush()

        self._qdisc_trace_cache[key] = tr
        return tr

    # convenience wrappers
    def read_mahi_trace(self, path: Path):
        tr = self.read_mahi_trace_full(path)
        return tr["t_ms"], _series_for_compare(tr, self.mode)

    def read_qdisc_trace(self, path: Path):
        tr = self.read_qdisc_trace_full(path)
        return tr["t_ms"], _series_for_compare(tr, self.mode)

    # ----------------------------
    # DTW compute
    # ----------------------------
    def compute_cross(self):
        q_files = self._get_qdisc_files()
        m_files = self._get_mahi_files()

        q_items = []
        for qf in q_files:
            qi = self._extract_id(qf)
            q_key = f"{qi}l"
            q_tr = self.read_qdisc_trace_full(qf)
            q_items.append((q_key, q_tr))

        m_items = []
        for mf in m_files:
            mi = self._extract_id(mf)
            m_key = f"{mi}m"
            m_tr = self.read_mahi_trace_full(mf)
            m_items.append((m_key, m_tr))

        jobs = [(qk, qt, mk, mt, self.mode) for (qk, qt), (mk, mt) in product(q_items, m_items)]

        self.cross_results = []
        self.cross_dist = {}

        with Pool(self.num_processes) as pool:
            for a, b, d in pool.imap_unordered(_cross_worker, jobs, chunksize=32):
                self.cross_results.append((a, b, d))
                self.cross_dist.setdefault(a, {})[b] = d

    def compute_qdisc_internal(self):
        q_files = self._get_qdisc_files()
        items = []
        for qf in q_files:
            qi = self._extract_id(qf)
            key = f"{qi}l"
            tr = self.read_qdisc_trace_full(qf)
            items.append((key, tr))

        jobs = []
        for i in range(len(items)):
            a_key, a_tr = items[i]
            for j in range(i + 1, len(items)):
                b_key, b_tr = items[j]
                jobs.append((a_key, a_tr, b_key, b_tr, self.mode))

        self.qdisc_internal = []
        self.qdisc_dist = {}

        with Pool(self.num_processes) as pool:
            for a, b, d in pool.imap_unordered(_internal_worker, jobs, chunksize=32):
                self.qdisc_internal.append((a, b, d))
                self.qdisc_dist.setdefault(a, {})[b] = d
                self.qdisc_dist.setdefault(b, {})[a] = d

    def compute_mahi_internal(self):
        m_files = self._get_mahi_files()
        items = []
        for mf in m_files:
            mi = self._extract_id(mf)
            key = f"{mi}m"
            tr = self.read_mahi_trace_full(mf)
            items.append((key, tr))

        jobs = []
        for i in range(len(items)):
            a_key, a_tr = items[i]
            for j in range(i + 1, len(items)):
                b_key, b_tr = items[j]
                jobs.append((a_key, a_tr, b_key, b_tr, self.mode))

        self.mahi_internal = []
        self.mahi_dist = {}

        with Pool(self.num_processes) as pool:
            for a, b, d in pool.imap_unordered(_internal_worker, jobs, chunksize=32):
                self.mahi_internal.append((a, b, d))
                self.mahi_dist.setdefault(a, {})[b] = d
                self.mahi_dist.setdefault(b, {})[a] = d

    # ----------------------------
    # Plotting (same API)
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

    def plot_overlay_queue_traces(self, dt_ms: int = 16, cutoff_ms: int = 0, show_legend: bool = False):
        mahi_files = self._get_mahi_files()
        qdisc_files = self._get_qdisc_files()

        fig, axes = plt.subplots(1, 2, figsize=(18, 5))

        global_ymin = float("inf")
        global_ymax = float("-inf")
        global_xmax = 0.0

        # ---- Mahimahi overlay ----
        ax = axes[0]
        for f in mahi_files:
            tr = self.read_mahi_trace_full(f)
            t_ms = tr["t_ms"]
            y = _series_for_compare(tr, self.mode)
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
        if show_legend:
            ax.legend(fontsize=7)

        # ---- Linux qdisc overlay ----
        ax = axes[1]
        for f in qdisc_files:
            tr = self.read_qdisc_trace_full(f)
            t_ms = tr["t_ms"]
            y = _series_for_compare(tr, self.mode)
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
        if show_legend:
            ax.legend(fontsize=7)

        if global_ymin != float("inf") and global_ymax != float("-inf"):
            for ax in axes:
                ax.set_ylim(global_ymin, global_ymax)
                ax.set_xlim(0, global_xmax)

        plt.tight_layout()
        return fig
