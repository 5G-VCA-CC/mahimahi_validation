from pathlib import Path
import re
from itertools import product
from multiprocessing import Pool, cpu_count
import matplotlib.pyplot as plt


class DTWAnalyzer:
    BACKLOG_RE = re.compile(r"\bbacklog\s+(\d+)b\s+(\d+)p\b")

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

        # file lists (avoid repeated glob/sort)
        self._qdisc_files = None
        self._mahi_files = None

    # ===============================================================
    # FILE LIST HELPERS
    # ===============================================================
    def _get_qdisc_files(self):
        if self._qdisc_files is None:
            self._qdisc_files = sorted(self.qdisc_dir.glob("qdisc_*.log"))
        return self._qdisc_files

    def _get_mahi_files(self):
        if self._mahi_files is None:
            self._mahi_files = sorted(self.mahi_dir.glob("output_*.txt"))
        return self._mahi_files

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
    # (updated to pick 2nd backlog per block if present; else 1st)
    # ===============================================================
    @staticmethod
    def _read_qdisc_bytes(path: Path):
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
    def _read_qdisc_packets(path: Path):
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

    # ===============================================================
    # UNIVERSAL READERS (switch based on mode)
    # + per-file memoization to avoid quadratic file rereads
    # ===============================================================
    def read_qdisc_series(self, path: Path):
        key = (str(path), self.mode)
        if key in self._qdisc_series_cache:
            return self._qdisc_series_cache[key]

        ys = self._read_qdisc_packets(path) if self.mode == "packets" else self._read_qdisc_bytes(path)
        self._qdisc_series_cache[key] = ys
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
    # WORKERS (use in-worker caches to avoid re-reading same files)
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

        qpairs = [(int(f.stem.split("_")[1]), f) for f in qdisc_files]
        mpairs = [(int(f.stem.split("_")[1]), f) for f in mahi_files]

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
        qpairs = [(int(f.stem.split("_")[1]), f) for f in qdisc_files]

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
        mpairs = [(int(f.stem.split("_")[1]), f) for f in mahi_files]

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
    # ===============================================================
    def plot_overlay_queue_traces(self, dt_ms: int = 16, cutoff_ms: int = 1000, show_legend: bool = False):
        cutoff_samples = cutoff_ms // dt_ms

        mahi_files = self._get_mahi_files()
        qdisc_files = self._get_qdisc_files()

        fig, axes = plt.subplots(1, 2, figsize=(18, 5))

        global_ymin = float("inf")
        global_ymax = float("-inf")
        global_xmax = 0

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
            global_xmax = max(global_xmax, t_ms[-1])

        ax.set_title(f"Mahimahi queue vs time (overlay, {self.mode})")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(f"Queue backlog ({self.mode})")
        ax.grid(True, alpha=0.2)
        if show_legend:
            ax.legend(fontsize=7)

        # ---- Linux qdisc overlay ----
        ax = axes[1]
        for f in qdisc_files:
            y = self.read_qdisc_series(f)
            if len(y) <= cutoff_samples:
                continue
            y = y[cutoff_samples:]
            t_ms = [(i + cutoff_samples) * dt_ms for i in range(len(y))]

            ax.plot(t_ms, y, linewidth=0.6, marker="o", markersize=2, label=f.stem)

            global_ymin = min(global_ymin, min(y))
            global_ymax = max(global_ymax, max(y))
            global_xmax = max(global_xmax, t_ms[-1])

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
