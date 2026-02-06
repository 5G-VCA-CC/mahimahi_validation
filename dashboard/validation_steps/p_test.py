import numpy as np
from multiprocessing import Pool, cpu_count
import matplotlib.pyplot as plt
from pathlib import Path


class NonparametricDTWTest:
    """
    Permutation test on a DTW-based statistic.

    Statistic used here (same as your original intent):
      S(A,B) = mean_{a in A, b in B} dist(a,b)

    Under H0 (exchangeability), labels are arbitrary.
    p-value (one-sided, "difference" test; larger => more different):
      p = P(S_perm >= S_orig)
    """

    def __init__(self, dist_dict, mahi_keys, qdisc_keys):
        self.dist = dist_dict
        self.mahi = sorted(mahi_keys, key=lambda x: int(x[:-1]))
        self.qdisc = sorted(qdisc_keys, key=lambda x: int(x[:-1]))

        # original statistic on true labels
        self.original_values = self._compute_between(self.mahi, self.qdisc)
        if self.original_values.size == 0:
            raise ValueError("Original A×B sample is empty; dist_dict missing required pairs.")
        self.original_mean = float(np.mean(self.original_values))

    # ---------------------------------------------------------
    # INTERNAL: Compute pairwise distances A×B using dist_dict
    # ---------------------------------------------------------
    def _compute_between(self, A, B):
        vals = []
        getA = self.dist.get

        for a in A:
            row = getA(a, None)
            if row is None:
                # maybe stored in reverse
                for b in B:
                    rowb = getA(b, None)
                    if rowb is not None and a in rowb:
                        vals.append(rowb[a])
                continue

            # forward lookup for each b
            for b in B:
                v = row.get(b, None)
                if v is not None:
                    vals.append(v)
                else:
                    # try reverse
                    rowb = getA(b, None)
                    if rowb is not None:
                        vb = rowb.get(a, None)
                        if vb is not None:
                            vals.append(vb)

        return np.asarray(vals, dtype=np.float64)

    # ---------------------------------------------------------
    # ONE RANDOM SHUFFLE (PURE FUNCTION for multiprocessing)
    # ---------------------------------------------------------
    @staticmethod
    def _shuffle_once(sizeA, all_keys, dist, original_mean, epsilon, seed):
        # independent RNG per shuffle
        rng = np.random.default_rng(seed)

        perm = rng.permutation(len(all_keys))
        keys = [all_keys[i] for i in perm]

        A = keys[:sizeA]
        B = keys[sizeA:]

        # compute S(A,B)
        vals = []
        getA = dist.get

        for a in A:
            row = getA(a, None)
            if row is None:
                for b in B:
                    rowb = getA(b, None)
                    if rowb is not None and a in rowb:
                        vals.append(rowb[a])
                continue

            for b in B:
                v = row.get(b, None)
                if v is not None:
                    vals.append(v)
                else:
                    rowb = getA(b, None)
                    if rowb is not None:
                        vb = rowb.get(a, None)
                        if vb is not None:
                            vals.append(vb)

        if not vals:
            # If this happens often, your dist_dict is missing many pairs and the test is biased.
            return (np.nan, 0)

        shuffled_mean = float(np.mean(vals))
        return (shuffled_mean, len(vals))

    # ---------------------------------------------------------
    # MAIN PERMUTATION TEST
    # ---------------------------------------------------------
    def run(self, num_shuffles=2000, epsilon=1e-9, verbose=True):
        """
        One-sided permutation test (difference test):
          p = P(S_perm >= S_orig - eps)

        - Uses independent per-shuffle RNG seeds (safe under multiprocessing)
        - Tracks sample sizes to detect missing-pair bias
        - Uses add-one smoothing for a well-behaved p-value
        """
        sizeA = len(self.mahi)
        all_keys = self.mahi + self.qdisc

        # pre-generate independent seeds
        ss = np.random.SeedSequence()
        child_seeds = ss.spawn(num_shuffles)
        seeds = [int(s.generate_state(1)[0]) for s in child_seeds]

        args = [(sizeA, all_keys, self.dist, self.original_mean, epsilon, seed) for seed in seeds]

        with Pool(processes=max(1, cpu_count() - 1)) as pool:
            out = pool.starmap(NonparametricDTWTest._shuffle_once, args)

        shuffled_means = np.array([x for (x, _) in out], dtype=np.float64)
        sample_sizes = np.array([k for (_, k) in out], dtype=np.int64)

        # drop any nans (should be rare; if frequent, dist_dict is incomplete and results are suspect)
        ok = ~np.isnan(shuffled_means)
        shuffled_means = shuffled_means[ok]
        sample_sizes = sample_sizes[ok]

        if shuffled_means.size == 0:
            raise ValueError("All shuffles returned empty/NaN samples; dist_dict likely incomplete.")

        # Correct tail for "difference" (larger statistic => more different)
        num_ge = int(np.sum(shuffled_means >= (self.original_mean - epsilon)))

        # add-one smoothing (recommended)
        p_value = (num_ge + 1) / (shuffled_means.size + 1)

        # diagnostics: expected pairs if dist_dict were complete
        expected_pairs = sizeA * (len(all_keys) - sizeA)

        self.results = {
            "original_mean": self.original_mean,
            "num_shuffles_requested": num_shuffles,
            "num_shuffles_used": int(shuffled_means.size),
            "num_ge": num_ge,
            "p_value": float(p_value),
            "shuffled_means": shuffled_means,
            "epsilon": float(epsilon),
            "expected_pairs_per_shuffle": int(expected_pairs),
            "median_pairs_found_per_shuffle": int(np.median(sample_sizes)),
            "min_pairs_found_per_shuffle": int(np.min(sample_sizes)),
            "max_pairs_found_per_shuffle": int(np.max(sample_sizes)),
        }

        if verbose:
            print(f"[perm] orig={self.original_mean:.6f}  p={p_value:.6g}  "
                  f"pairs_found_median={self.results['median_pairs_found_per_shuffle']}/"
                  f"{expected_pairs}")

        return self.results

    # ---------------------------------------------------------
    # VISUALIZATION
    # ---------------------------------------------------------
    def plot(self, *, show: bool = False, save_path: str | None = None, dpi: int = 200):
        if not hasattr(self, "results"):
            print("You must run the permutation test first.")
            return None

        shuffled = np.asarray(self.results["shuffled_means"], dtype=np.float64)
        orig = float(self.results["original_mean"])
        p = float(self.results["p_value"])

        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        # --------------------------- HISTOGRAM ---------------------------
        ax = axes[0]
        ax.hist(shuffled, bins=25, alpha=0.7, edgecolor="black")
        ax.axvline(orig, linestyle="--", linewidth=2,
                label=f"Observed = {orig:.4f}")

        ax.set_title("Permutation Test — Histogram")
        ax.set_xlabel("Mean DTW")
        ax.set_ylabel("Frequency")
        ax.legend()
        ax.text(0.05, 0.95, f"p = {p:.6g}", transform=ax.transAxes,
                fontsize=12, verticalalignment="top")

        # --------------------------- CDF ---------------------------
        ax = axes[1]
        sorted_vals = np.sort(shuffled)
        if len(sorted_vals) <= 1:
            cdf = np.array([1.0] * len(sorted_vals))
        else:
            cdf = np.arange(len(sorted_vals)) / (len(sorted_vals) - 1)

        ax.plot(sorted_vals, cdf, label="Shuffled CDF")
        ax.axvline(orig, linestyle="--",
                label=f"Observed = {orig:.4f}")

        ax.set_title("Permutation Test — CDF")
        ax.set_xlabel("Mean DTW")
        ax.set_ylabel("CDF")
        ax.legend()

        plt.tight_layout()

        if save_path:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(save_path, dpi=dpi, bbox_inches="tight")
            print(f"Saved: {Path(save_path).resolve()}")

        if show:
            plt.show()
        else:
            plt.close(fig)

        # --------------------------- Interpretation -----------------------
        print("\n================ Interpretation ================")
        print("One-sided permutation test for DIFFERENCE:")
        print("  p = P(S_perm ≥ S_obs)  (larger statistic = more different)")
        if p < 0.05:
            print("Result: evidence of DIFFERENCE.")
        else:
            print("Result: no strong evidence of difference.")
        print("\nDiagnostics:")
        print(f"  expected pairs per shuffle: {self.results['expected_pairs_per_shuffle']}")
        print(f"  pairs found (median/min/max): "
            f"{self.results['median_pairs_found_per_shuffle']}/"
            f"{self.results['min_pairs_found_per_shuffle']}/"
            f"{self.results['max_pairs_found_per_shuffle']}")
        print("================================================")

        return fig
