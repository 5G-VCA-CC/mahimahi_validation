from pathlib import Path
import re
from itertools import product
from multiprocessing import Pool, cpu_count
import matplotlib.pyplot as plt


class DTWAnalyzer:
    BACKLOG_RE = re.compile(r"\bbacklog\s+(\d+)b\s+(\d+)p\b")
    _ID_AT_END = re.compile(r"_(\d+)$")

    # NEW: TS_NS block header (from your updated bash logger)
    TS_NS_RE = re.compile(r"^TS_NS\s+(\d+)\s*$")

    def __init__(self, qdisc_dir, mahi_dir, cache_file, mode="packets"):
        """
        mode = "bytes" or "packets"
        """
        self.qdisc_dir = Path(qdisc_dir)
        self.mahi_dir = Path(mahi_dir)
        self.cache_file = Path(cache_file)
        self.mode = mode
        self.num_processes = max(1, cpu_count() - 1)

        # results
        self.cross_results = []
        self.qdisc_internal = []
        self.mahi_internal = []

        # lookup dicts
        self.cross_dist = {}
        self.qdisc_dist = {}
        self.mahi_dist = {}

        # parsed-series caches (avoid quadratic file rereads)
        self._qdisc_series_cache = {}
        self._mahi_series_cache = {}

        # NEW: time-aware qdisc trace cache: (t_ms, y)
        self._qdisc_trace_cache = {}

        # file lists (avoid repeated glob/sort)
        self._qdisc_files = None
        self._mahi_files = None

    # ===============================================================
    # FILE LIST HELPERS
    # ===============================================================
    def _get_qdisc_files(self):
        if self._qdisc_files is None:
            # matches: qdisc_foo.log, qdisc_classic_1.log, qdisc_l4s_run3.log, etc.
            self._qdisc_files = sorted(self.qdisc_dir.glob("qdisc_*"))
        return self._qdisc_files

    def _get_mahi_files(self):
        if self._mahi_files is None:
            # matches: output_1.txt, output_classic_1.txt, output_l4s_run3.txt, etc.
            self._mahi_files = sorted(self.mahi_dir.glob("output_*"))
        return self._mahi_files

    # ===============================================================
    # filename id extractor
    # expects the stem to end with _<number>, e.g. output_classic_1, qdisc_l4s_12
    # ===============================================================
    def _extract_id(self, path: Path) -> int:
        m = self._ID_AT_END.search(path.stem)
        if not m:
            raise ValueError(f"Expected filename to end with _<number>: {path.name}")
        return int(m.group(1))

    # ===============================================================
    # CACHE LOADING
    # ===============================================================
    def load_cache(self):
        if not self.cache_file.exists():
            return False

        print(f"Loading cached DTW from {self.cache_file}")

        with open(self.cache_file, "r") as f:
            for line in f:
                a, b, d = line.strip().split(",")
                d = float(d)

                # cross mode
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

        print("Loaded cache OK.\n")
        return True

    # ===============================================================
    # QDISC PARSERS
    # - "series" readers for DTW (y only)
    # - "trace" reader for plotting (t_ms + y) using TS_NS
    # ===============================================================
    @staticmethod
    def _read_qdisc_packets_legacy(path: Path):
        """
        Old format: uses '------ date ------' separators.
        Kept for backwards compatibility if you still have old logs.
        """
        ys = []
        block_vals = []

        with open(path, "r", errors="ignore") as f:
            for line in f:
                if re.match(r"^------ .+ ------\s*$", line):
                    if block_vals:
                        ys.append(block_vals[1] if len(block_vals) >= 2 else block_vals[0])
                    block_vals = []
                    continue

                m = DTWAnalyzer.BACKLOG_RE.search(line)
                if m:
                    block_vals.append(int(m.group(2)))  # packets

        if block_vals:
            ys.append(block_vals[1] if len(block_vals) >= 2 else block_vals[0])

        return ys

    @staticmethod
    def _read_qdisc_bytes_legacy(path: Path):
        """
        Old format: uses '------ date ------' separators.
        """
        ys = []
        block_vals = []

        with open(path, "r", errors="ignore") as f:
            for line in f:
                if re.match(r"^------ .+ ------\s*$", line):
                    if block_vals:
                        ys.append(block_vals[1] if len(block_vals) >= 2 else block_vals[0])
                    block_vals = []
                    continue

                m = DTWAnalyzer.BACKLOG_RE.search(line)
                if m:
                    block_vals.append(int(m.group(1)))  # bytes

        if block_vals:
            ys.append(block_vals[1] if len(block_vals) >= 2 else block_vals[0])

        return ys

    @staticmethod
    def _read_qdisc_trace_tsns(path: Path, mode: str):
        """
        New format: each block begins with:
            TS_NS <monotonic_ns>

        Returns:
            t_ms: elapsed ms since first TS_NS
            y:    backlog (packets or bytes)
        We still pick the 2nd backlog in each block if present (dualpi2 after htb),
        else the 1st backlog.
        """
        t_ms = []
        ys = []

        cur_ts = None
        block_vals = []
        t0 = None

        def flush():
            nonlocal cur_ts, block_vals, t0
            if cur_ts is None:
                return
            if not block_vals:
                return

            val = block_vals[1] if len(block_vals) >= 2 else block_vals[0]
            if t0 is None:
                t0 = cur_ts
            t_ms.append((cur_ts - t0) / 1e6)  # ns -> ms
            ys.append(val)

        with open(path, "r", errors="ignore") as f:
            for line in f:
                m_ts = DTWAnalyzer.TS_NS_RE.match(line)
                if m_ts:
                    flush()
                    cur_ts = int(m_ts.group(1))
                    block_vals = []
                    continue

                m = DTWAnalyzer.BACKLOG_RE.search(line)
                if m:
                    block_vals.append(int(m.group(2) if mode == "packets" else m.group(1)))

        flush()
        return t_ms, ys

    def read_qdisc_trace(self, path: Path):
        """
        Preferred for plotting qdisc: uses TS_NS if present.
        Caches (t_ms, y).
        """
        key = (str(path), self.mode)
        if key in self._qdisc_trace_cache:
            return self._qdisc_trace_cache[key]

        # detect TS_NS quickly
        has_ts = False
        with open(path, "r", errors="ignore") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                if self.TS_NS_RE.match(line):
                    has_ts = True
                    break

        if has_ts:
            t_ms, y = self._read_qdisc_trace_tsns(path, self.mode)
        else:
            # no timestamps -> fallback to synthetic dt later if needed
            t_ms, y = [], self.read_qdisc_series(path)

        self._qdisc_trace_cache[key] = (t_ms, y)
        return t_ms, y

    def read_qdisc_series(self, path: Path):
        """
        DTW uses y-only series.
        If TS_NS logs exist, reuse trace and drop time.
        """
        key = (str(path), self.mode)
        if key in self._qdisc_series_cache:
            return self._qdisc_series_cache[key]

        # Try TS_NS trace first
        t_ms, y = self.read_qdisc_trace(path)
        if y:
            self._qdisc_series_cache[key] = y
            return y

        # Fallback (shouldn't happen)
        ys = self._read_qdisc_packets_legacy(path) if self.mode == "packets" else self._read_qdisc_bytes_legacy(path)
        self._qdisc_series_cache[key] = ys
        return ys

    # ===============================================================
    # MAHIMAHI PARSERS
    # ===============================================================
    @staticmethod
    def _read_mahi_bytes(path: Path):
        ys = []
        with open(path, "r", errors="ignore") as f:
            for line in f:
                if "queue size in bytes:" in line:
                    try:
                        ys.append(int(line.split(":")[-1].strip()))
                    except:
                        pass
        return ys

    @staticmethod
    def _read_mahi_packets(path: Path):
        ys = []
        with open(path, "r", errors="ignore") as f:
            for line in f:
                if "queue size in packets:" in line:
                    try:
                        ys.append(int(line.split(":")[-1].strip()))
                    except:
                        pass
        return ys

    def read_mahi_series(self, path: Path):
        key = (str(path), self.mode)
        if key in self._mahi_series_cache:
            return self._mahi_series_cache[key]

        ys = self._read_mahi_packets(path) if self.mode == "packets" else self._read_mahi_bytes(path)
        self._mahi_series_cache[key] = ys
        return ys

    # ===============================================================
    # DTW
    # ===============================================================
    @staticmethod
    def dtw_distance(a, b):
        n, m = len(a), len(b)
        if n == 0 or m == 0:
            return float(abs(sum(a) - sum(b)))

        INF = float("inf")
        prev = [INF] * (m + 1)
        curr = [INF] * (m + 1)
        prev[0] = 0

        for i in range(1, n + 1):
            curr[0] = INF
            ai = a[i - 1]
            for j in range(1, m + 1):
                bj = b[j - 1]
                cost = abs(ai - bj)
                curr[j] = cost + min(prev[j], curr[j - 1], prev[j - 1])
            prev, curr = curr, prev

        return prev[m]

    # ===============================================================
    # WORKERS
    # ===============================================================
    def _compute_cross_worker(self, pair):
        qi, qf, oi, of = pair
        a = self.read_qdisc_series(qf)
        b = self.read_mahi_series(of)
        if not a or not b:
            return None
        d = self.dtw_distance(a, b) / max(len(a), len(b))
        return (f"{qi}l", f"{oi}m", d)

    def _compute_qdisc_worker(self, pair):
        i, fi, j, fj = pair
        a = self.read_qdisc_series(fi)
        b = self.read_qdisc_series(fj)
        if not a or not b:
            return None
        d = self.dtw_distance(a, b) / max(len(a), len(b))
        return (f"{i}l", f"{j}l", d)

    def _compute_mahi_worker(self, pair):
        i, fi, j, fj = pair
        a = self.read_mahi_series(fi)
        b = self.read_mahi_series(fj)
        if not a or not b:
            return None
        d = self.dtw_distance(a, b) / max(len(a), len(b))
        return (f"{i}m", f"{j}m", d)

    # ===============================================================
    # COMPUTE
    # ===============================================================
    def compute_cross(self):
        qdisc_files = self._get_qdisc_files()
        mahi_files = self._get_mahi_files()

        qpairs = [(self._extract_id(f), f) for f in qdisc_files]
        mpairs = [(self._extract_id(f), f) for f in mahi_files]

        jobs = [(qi, qf, oi, of) for (qi, qf), (oi, of) in product(qpairs, mpairs)]
        print(f"Computing {len(jobs)} cross-mode DTW pairs...")

        with Pool(self.num_processes) as pool:
            for result in pool.imap_unordered(self._compute_cross_worker, jobs):
                if result:
                    self.cross_results.append(result)

        for a, b, d in self.cross_results:
            self.cross_dist.setdefault(a, {})[b] = d

    def compute_qdisc_internal(self):
        qdisc_files = self._get_qdisc_files()
        qpairs = [(self._extract_id(f), f) for f in qdisc_files]

        jobs = []
        for (i, fi) in qpairs:
            for (j, fj) in qpairs:
                if j <= i:
                    continue
                jobs.append((i, fi, j, fj))

        print(f"Computing {len(jobs)} qdisc internal pairs...")

        with Pool(self.num_processes) as pool:
            for result in pool.imap_unordered(self._compute_qdisc_worker, jobs):
                if result:
                    self.qdisc_internal.append(result)

        for a, b, d in self.qdisc_internal:
            self.qdisc_dist.setdefault(a, {})[b] = d
            self.qdisc_dist.setdefault(b, {})[a] = d

    def compute_mahi_internal(self):
        mahi_files = self._get_mahi_files()
        mpairs = [(self._extract_id(f), f) for f in mahi_files]

        jobs = []
        for (i, fi) in mpairs:
            for (j, fj) in mpairs:
                if j <= i:
                    continue
                jobs.append((i, fi, j, fj))

        print(f"Computing {len(jobs)} mahimahi internal pairs...")

        with Pool(self.num_processes) as pool:
            for result in pool.imap_unordered(self._compute_mahi_worker, jobs):
                if result:
                    self.mahi_internal.append(result)

        for a, b, d in self.mahi_internal:
            self.mahi_dist.setdefault(a, {})[b] = d
            self.mahi_dist.setdefault(b, {})[a] = d

    # ===============================================================
    # CACHE SAVE
    # ===============================================================
    def save_cache(self):
        with open(self.cache_file, "w") as f:
            for a, b, d in self.cross_results:
                f.write(f"{a},{b},{d}\n")
            for a, b, d in self.qdisc_internal:
                f.write(f"{a},{b},{d}\n")
            for a, b, d in self.mahi_internal:
                f.write(f"{a},{b},{d}\n")

        print(f"Saved DTW cache → {self.cache_file}")

    # ===============================================================
    # TRIPLE HISTOGRAM
    # ===============================================================
    @staticmethod
    def _extract_values(pairs):
        return [d for (_, _, d) in pairs]

    def plot_triple_hist(self):
        qvals = self._extract_values(self.qdisc_internal)
        mvals = self._extract_values(self.mahi_internal)
        cvals = self._extract_values(self.cross_results)

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        axes[0].hist(qvals, bins=30, edgecolor="black")
        axes[0].set_title("Qdisc DTW Histogram")

        axes[1].hist(mvals, bins=30, edgecolor="black")
        axes[1].set_title("Mahimahi DTW Histogram")

        axes[2].hist(cvals, bins=30, edgecolor="black")
        axes[2].set_title("Cross-Mode DTW Histogram")

        plt.tight_layout()
        return fig

    # ===============================================================
    # TRIPLE CDF
    # ===============================================================
    def plot_triple_cdf(self):
        qvals = sorted(self._extract_values(self.qdisc_internal))
        mvals = sorted(self._extract_values(self.mahi_internal))
        cvals = sorted(self._extract_values(self.cross_results))

        fig, axes = plt.subplots(1, 3, figsize=(18, 5))

        if qvals:
            axes[0].plot(qvals, [i / len(qvals) for i in range(len(qvals))])
        axes[0].set_title("Qdisc DTW CDF")

        if mvals:
            axes[1].plot(mvals, [i / len(mvals) for i in range(len(mvals))])
        axes[1].set_title("Mahimahi DTW CDF")

        if cvals:
            axes[2].plot(cvals, [i / len(cvals) for i in range(len(cvals))])
        axes[2].set_title("Cross-Mode DTW CDF")

        plt.tight_layout()
        return fig

    # ===============================================================
    # OVERLAID QUEUE PLOTS (Mahimahi vs Linux qdisc)
    # - Mahimahi: still uniform dt_ms (it truly is 16ms in your trace)
    # - Qdisc: uses TS_NS real timestamps (ms)
    # ===============================================================
    def plot_overlay_queue_traces(self, dt_ms: int = 16, cutoff_ms: int = 1000, show_legend: bool = False):
        cutoff_samples = cutoff_ms // dt_ms

        mahi_files = self._get_mahi_files()
        qdisc_files = self._get_qdisc_files()

        fig, axes = plt.subplots(1, 2, figsize=(18, 5))

        global_ymin = float("inf")
        global_ymax = float("-inf")
        global_xmax = 0.0

        # ---- Mahimahi overlay ----
        ax = axes[0]
        for f in mahi_files:
            y = self.read_mahi_series(f)
            if len(y) <= cutoff_samples:
                continue
            y = y[cutoff_samples:]
            t_ms = [(i + cutoff_samples) * dt_ms for i in range(len(y))]

            ax.plot(t_ms, y, linewidth=0.6, marker="o", markersize=2, label=f.stem)

            global_ymin = min(global_ymin, min(y))
            global_ymax = max(global_ymax, max(y))
            global_xmax = max(global_xmax, float(t_ms[-1]))

        ax.set_title(f"Mahimahi queue vs time (overlay, {self.mode})")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(f"Queue backlog ({self.mode})")
        ax.grid(True, alpha=0.2)
        if show_legend:
            ax.legend(fontsize=7)

        # ---- Linux qdisc overlay (REAL TIME) ----
        ax = axes[1]
        for f in qdisc_files:
            t_ms, y = self.read_qdisc_trace(f)
            if not t_ms or not y:
                continue

            # cutoff by actual time (ms), not sample count
            start_idx = 0
            while start_idx < len(t_ms) and t_ms[start_idx] < cutoff_ms:
                start_idx += 1

            t2 = t_ms[start_idx:]
            y2 = y[start_idx:]
            if not y2:
                continue

            ax.plot(t2, y2, linewidth=0.6, marker="o", markersize=2, label=f.stem)

            global_ymin = min(global_ymin, min(y2))
            global_ymax = max(global_ymax, max(y2))
            global_xmax = max(global_xmax, float(t2[-1]))

        ax.set_title(f"Linux qdisc queue vs time (overlay, {self.mode})")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(f"Queue backlog ({self.mode})")
        ax.grid(True, alpha=0.2)
        if show_legend:
            ax.legend(fontsize=7)

        # ---- FORCE SAME AXES ----
        for ax in axes:
            ax.set_ylim(global_ymin, global_ymax)
            ax.set_xlim(0, global_xmax)

        plt.tight_layout()
        return fig