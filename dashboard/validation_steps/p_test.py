import numpy as np
import random
from multiprocessing import Pool, cpu_count
from functools import partial
import matplotlib.pyplot as plt


class ParametricDTWTest:
    """
    Permutation test for DTW distances between
    Mahimahi (M) and Qdisc (Q) groups.

    Computes:
      - original mean DTW(M,Q)
      - many shuffled means
      - p-value = P(shuffled_mean < original_mean - epsilon)
    """

    def __init__(self, dist_dict, mahi_keys, qdisc_keys):
        self.dist = dist_dict
        self.mahi = sorted(mahi_keys, key=lambda x: int(x[:-1]))
        self.qdisc = sorted(qdisc_keys, key=lambda x: int(x[:-1]))

        # compute original DTW(M, Q) mean
        self.original_values = self._compute_between(self.mahi, self.qdisc)
        self.original_mean = float(np.mean(self.original_values))

    # ---------------------------------------------------------
    # INTERNAL: Compute pairwise distances A×B
    # ---------------------------------------------------------
    def _compute_between(self, A, B):
        vals = []
        for a in A:
            for b in B:
                if b in self.dist.get(a, {}):
                    vals.append(self.dist[a][b])
                elif a in self.dist.get(b, {}):
                    vals.append(self.dist[b][a])
        return np.array(vals, dtype=float)

    # ---------------------------------------------------------
    # ONE RANDOM SHUFFLE
    # ---------------------------------------------------------
    def _shuffle_once(self, sizeA, all_keys):
        random.shuffle(all_keys)
        A = all_keys[:sizeA]
        B = all_keys[sizeA:]
        sample = self._compute_between(A, B)
        return float(np.mean(sample)) if len(sample) > 0 else 0.0

    # ---------------------------------------------------------
    # MAIN PERMUTATION TEST
    # ---------------------------------------------------------
    def run(self, num_shuffles=200, epsilon=1e-9):

        sizeA = len(self.mahi)
        all_keys = self.mahi + self.qdisc
        self.epsilon = epsilon   # <--- FIXED: store epsilon so plot() can use it

        worker = partial(self._shuffle_once, sizeA)

        with Pool(processes=max(1, cpu_count() - 1)) as pool:
            shuffled_means = pool.map(worker, [all_keys.copy() for _ in range(num_shuffles)])

        # Count how many shuffled >= original mean (within epsilon tolerance)
        num_greater = sum(1 for m in shuffled_means if m >= self.original_mean - epsilon)
        num_less = num_shuffles - num_greater

        self.results = {
            "original_mean": self.original_mean,
            "num_greater": num_greater,
            "num_less": num_less,
            "p_value": num_less / num_shuffles,
            "shuffled_means": shuffled_means,
            "epsilon": epsilon
        }

        return self.results

    # ---------------------------------------------------------
    # VISUALIZATION
    # ---------------------------------------------------------
    def plot(self):
        """
        Plot histogram + CDF of shuffled means,
        and show where original mean − epsilon lies.
        """

        if not hasattr(self, "results"):
            print("You must run the permutation test first.")
            return

        shuffled = np.array(self.results["shuffled_means"])
        orig = self.results["original_mean"]
        eps = self.results["epsilon"]    # <--- FIXED
        p = self.results["p_value"]

        threshold = orig - eps

        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        # --------------------------- HISTOGRAM ---------------------------
        ax = axes[0]
        ax.hist(shuffled, bins=25, alpha=0.7, color="skyblue", edgecolor="black")

        ax.axvline(threshold, color="red", linestyle="--", linewidth=2,
                   label=f"Original − ε = {threshold:.4f}")

        ax.set_title("Permutation Test — Histogram")
        ax.set_xlabel("Mean DTW")
        ax.set_ylabel("Frequency")
        ax.legend()
        ax.text(0.05, 0.95, f"p = {p:.5f}", transform=ax.transAxes,
                fontsize=12, verticalalignment="top")

        # --------------------------- CDF ---------------------------
        ax = axes[1]
        sorted_vals = np.sort(shuffled)
        cdf = np.arange(len(sorted_vals)) / (len(sorted_vals) - 1)

        ax.plot(sorted_vals, cdf, color="blue", label="Shuffled CDF")
        ax.axvline(threshold, color="red", linestyle="--",
                   label=f"Original − ε = {threshold:.4f}")

        ax.set_title("Permutation Test — CDF")
        ax.set_xlabel("Mean DTW")
        ax.set_ylabel("CDF")
        ax.legend()

        plt.tight_layout()
        plt.show()

        # --------------------------- Interpretation -----------------------
        print("\n================ Interpretation ================")
        if p < 0.05:
            print("SIGNIFICANT DIFFERENCE detected (p < 0.05).")
        elif p > 0.95:
            print("STRONG EVIDENCE OF EQUIVALENCE (p > 0.95):\n"
                  "Random mixes rarely produce a smaller DTW.\n"
                  "Mahimahi and Qdisc appear statistically similar.")
        else:
            print("No statistical evidence of difference or equivalence.")
        print("================================================")

        return fig
