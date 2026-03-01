# validation_steps/dtw_analyzer.py
from __future__ import annotations

from pathlib import Path
import re
from multiprocessing import Pool, cpu_count

import numpy as np
from tqdm import tqdm

from .dtw_core import (
    CUTOFF_FRONT_MS,
    CUTOFF_BACK_MS,
    cutoff_series,
    dtw_distance_norm,
    time_and_series_for_compare,
)
from .trace_parsers import (
    read_mahi_trace_full_worker,
    read_qdisc_trace_full_worker,
)

# ============================================================
# Worker mode global (set by Pool initializer)
# ============================================================
_G_MODE = None


def _pool_init(mode: str):
    global _G_MODE
    _G_MODE = mode


# ============================================================
# Worker tasks (path-based DTW computation)
# ============================================================
def _cross_worker_paths(args):
    q_key, q_path, m_key, m_path = args
    q_tr = read_qdisc_trace_full_worker(q_path)
    m_tr = read_mahi_trace_full_worker(m_path)

    a = cutoff_series(q_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)
    b = cutoff_series(m_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)

    d = dtw_distance_norm(a, b)
    return (q_key, m_key, d)


def _internal_worker_paths(args):
    a_key, a_path, b_key, b_path = args

    if a_key.endswith("l"):
        a_tr = read_qdisc_trace_full_worker(a_path)
    else:
        a_tr = read_mahi_trace_full_worker(a_path)

    if b_key.endswith("l"):
        b_tr = read_qdisc_trace_full_worker(b_path)
    else:
        b_tr = read_mahi_trace_full_worker(b_path)

    a = cutoff_series(a_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)
    b = cutoff_series(b_tr, _G_MODE, cutoff_front_ms=CUTOFF_FRONT_MS, cutoff_back_ms=CUTOFF_BACK_MS)

    d = dtw_distance_norm(a, b)
    return (a_key, b_key, d)


# ============================================================
# DTWAnalyzer (compute/cache only)
# ============================================================
class DTWAnalyzer:
    _ID_AT_END = re.compile(r"_(\d+)$")

    def __init__(self, qdisc_dir, mahi_dir, mode="packets"):
        self.qdisc_dir = Path(qdisc_dir)
        self.mahi_dir = Path(mahi_dir)
        self.mode = mode  # packets | bytes | ecn_mark | t_ms | packet_dropped_* | qdelay_*_ms

        self.cache_file = Path(__file__).resolve().parent / "caches" / f"dtw_cache_{self.mode}.txt"        
        self.num_processes = max(1, cpu_count())

        # Raw list form: list[(keyA, keyB, dist)]
        self.cross_results = []
        self.qdisc_internal = []
        self.mahi_internal = []

        # Dict form for fast lookup: dist[a][b] = value
        self.cross_dist = {}   # qdisc key -> (mahi key -> dist)
        self.qdisc_dist = {}   # qdisc key -> (qdisc key -> dist) (symmetric)
        self.mahi_dist = {}    # mahi key  -> (mahi  key  -> dist) (symmetric)

        # Optional trace caches (overlay plotting can live elsewhere, but cheap to keep)
        self._qdisc_trace_cache = {}
        self._mahi_trace_cache = {}

    # ============================================================
    # File discovery helpers
    # ============================================================
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

    # ============================================================
    # Cache IO
    # ============================================================
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

    # ============================================================
    # Trace reading (optional convenience)
    # ============================================================
    def read_mahi_trace_full(self, path: Path):
        key = str(path)
        if key in self._mahi_trace_cache:
            return self._mahi_trace_cache[key]
        tr = read_mahi_trace_full_worker(key)
        self._mahi_trace_cache[key] = tr
        return tr

    def read_qdisc_trace_full(self, path: Path):
        key = str(path)
        if key in self._qdisc_trace_cache:
            return self._qdisc_trace_cache[key]
        tr = read_qdisc_trace_full_worker(key)
        self._qdisc_trace_cache[key] = tr
        return tr

    # ============================================================
    # DTW computations
    # ============================================================
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

    # ============================================================
    # Small utilities (keys, value extraction)
    # ============================================================
    @staticmethod
    def extract_values(pairs):
        return [d for (_, _, d) in pairs]

    def qdisc_keys(self):
        ks = set(self.qdisc_dist.keys())
        if not ks and self.qdisc_internal:
            for a, b, _ in self.qdisc_internal:
                ks.add(a)
                ks.add(b)
        return sorted(ks)

    def mahi_keys(self):
        ks = set(self.mahi_dist.keys())
        if not ks and self.mahi_internal:
            for a, b, _ in self.mahi_internal:
                ks.add(a)
                ks.add(b)
        return sorted(ks)

    # Expose for overlay use elsewhere (optional)
    def time_and_series_for_compare(self, trace: dict):
        return time_and_series_for_compare(trace, self.mode)