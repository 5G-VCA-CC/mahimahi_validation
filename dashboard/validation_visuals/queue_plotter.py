import os
import random
import numpy as np
import matplotlib.pyplot as plt


class QueueOccupancyPlotter:

    def __init__(self, qdisc_dir, mahi_dir):
        self.qdisc_dir = qdisc_dir
        self.mahi_dir  = mahi_dir

        self.qdisc_files = self._load_files(qdisc_dir)
        self.mahi_files  = self._load_files(mahi_dir)

    # ---------------------------------------------------------
    # Load all queue log files where each line = "time qlen_bytes"
    # ---------------------------------------------------------
    def _load_files(self, path):
        if not os.path.isdir(path):
            return []
        files = [os.path.join(path, f) for f in os.listdir(path)
                 if f.endswith(".log") or f.endswith(".txt")]
        return sorted(files)

    # ---------------------------------------------------------
    # Parse a queue log
    # ---------------------------------------------------------
    def _parse_file(self, filename):
        ts = []
        qs = []

        with open(filename, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                try:
                    t = float(parts[0])
                    q = float(parts[1])
                    ts.append(t / 0.016)  # convert to approx 16ms ticks
                    qs.append(q)
                except:
                    continue

        return np.array(ts), np.array(qs)

    # =========================================================
    # RANDOM SINGLE QDISC
    # =========================================================
    def plot_random_qdisc(self):
        if not self.qdisc_files:
            print("No QDISC logs found.")
            return

        f = random.choice(self.qdisc_files)
        ts, qs = self._parse_file(f)

        plt.figure(figsize=(10, 4))
        plt.plot(ts, qs, color="red")
        plt.title(f"Random QDISC Run\n{os.path.basename(f)}")
        plt.xlabel("Time (ticks of 16ms)")
        plt.ylabel("Queue (bytes)")
        plt.grid(True)
        plt.tight_layout()
        plt.show()

    # =========================================================
    # RANDOM SINGLE MAHIMAHI
    # =========================================================
    def plot_random_mahi(self):
        if not self.mahi_files:
            print("No MAHIMAHI logs found.")
            return

        f = random.choice(self.mahi_files)
        ts, qs = self._parse_file(f)

        plt.figure(figsize=(10, 4))
        plt.plot(ts, qs, color="blue")
        plt.title(f"Random Mahimahi Run\n{os.path.basename(f)}")
        plt.xlabel("Time (ticks of 16ms)")
        plt.ylabel("Queue (bytes)")
        plt.grid(True)
        plt.tight_layout()
        plt.show()

    # =========================================================
    # RANDOM QDISC + RANDOM MAHI SIDE-BY-SIDE
    # =========================================================
    def plot_random_pair(self):
        if not self.qdisc_files or not self.mahi_files:
            print("Missing files for side-by-side comparison.")
            return

        fq = random.choice(self.qdisc_files)
        fm = random.choice(self.mahi_files)

        ts_q, q_q = self._parse_file(fq)
        ts_m, q_m = self._parse_file(fm)

        fig, ax = plt.subplots(1, 2, figsize=(14, 4))

        ax[0].plot(ts_q, q_q, color="red")
        ax[0].set_title(f"QDISC\n{os.path.basename(fq)}")
        ax[0].set_xlabel("Time (ticks of 16ms)")
        ax[0].set_ylabel("Queue (bytes)")
        ax[0].grid(True)

        ax[1].plot(ts_m, q_m, color="blue")
        ax[1].set_title(f"Mahimahi\n{os.path.basename(fm)}")
        ax[1].set_xlabel("Time (ticks of 16ms)")
        ax[1].set_ylabel("Queue (bytes)")
        ax[1].grid(True)

        plt.tight_layout()
        plt.show()
