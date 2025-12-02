import numpy as np
from sklearn_extra.cluster import KMedoids
from sklearn.manifold import MDS
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D


class Cluster_DTW:

    def __init__(self, dist_dict):
        self.dist_dict = dist_dict
        self.keys = sorted(dist_dict.keys(), key=lambda x: int(x[:-1]))
        self.k = 2
        self.D = self._to_matrix()
        self._cluster()

    def _to_matrix(self):
        n = len(self.keys)
        D = np.zeros((n, n), dtype=float)
        for i, ki in enumerate(self.keys):
            for j, kj in enumerate(self.keys):
                if ki == kj:
                    D[i, j] = 0
                else:
                    if kj in self.dist_dict.get(ki, {}):
                        D[i, j] = self.dist_dict[ki][kj]
                    elif ki in self.dist_dict.get(kj, {}):
                        D[i, j] = self.dist_dict[kj][ki]
                    else:
                        D[i, j] = 0
        return D

    def _cluster(self):
        model = KMedoids(
            n_clusters=self.k,
            metric="precomputed",
            init="k-medoids++",
            random_state=42
        )
        model.fit(self.D)

        self.medoids = [self.keys[idx] for idx in model.medoid_indices_]
        self.labels = {key: int(label) for key, label in zip(self.keys, model.labels_)}

    def summary(self):
        print("\n=== K-Medoids DTW Clustering Summary ===")
        print(f"Medoids: {self.medoids}")
        print("\nAssignments:")
        for key, label in self.labels.items():
            print(f"{key}: cluster {label}")

    def visualize(self):
        mds = MDS(
            n_components=2,
            dissimilarity='precomputed',
            random_state=42
        )
        coords = mds.fit_transform(self.D)

        plt.figure(figsize=(10, 8))

        # Map cluster to marker shapes
        shape_map = {
            0: "o",   # circle
            1: "^",   # triangle
        }

        # Plot each point
        for i, key in enumerate(self.keys):
            cluster_label = self.labels[key]
            marker_shape = shape_map[cluster_label]

            # Color = type
            if key.endswith("m"):
                color = "blue"       # mahimahi
            else:
                color = "pink"       # qdisc

            plt.scatter(
                coords[i, 0],
                coords[i, 1],
                color=color,
                marker=marker_shape,
                s=100,
                edgecolors="black",
                linewidth=0.7
            )

            # Add label next to point
            plt.text(
                coords[i, 0] + 0.015,
                coords[i, 1] + 0.015,
                key,
                fontsize=10
            )

        # Draw medoids
        for med in self.medoids:
            idx = self.keys.index(med)
            plt.scatter(
                coords[idx, 0],
                coords[idx, 1],
                marker="X",
                color="black",
                s=250,
                edgecolors="white",
                linewidth=1.4,
                label="Medoid" if med == self.medoids[0] else None
            )

        # ------------------------------
        # Construct Legend
        # ------------------------------
        legend_elements = [
            Line2D([0], [0], marker='o', color='w', markerfacecolor='pink',
                   markeredgecolor='black', markersize=12, label='Qdisc (pink)'),
            Line2D([0], [0], marker='o', color='w', markerfacecolor='blue',
                   markeredgecolor='black', markersize=12, label='Mahimahi (blue)'),
            Line2D([0], [0], marker='o', color='w', markerfacecolor='gray',
                   markeredgecolor='black', markersize=12, label='Cluster 0 (circle)'),
            Line2D([0], [0], marker='^', color='w', markerfacecolor='gray',
                   markeredgecolor='black', markersize=12, label='Cluster 1 (triangle)'),
            Line2D([0], [0], marker='X', color='black', markersize=12, label='Medoid')
        ]

        plt.legend(handles=legend_elements, loc="best")
        plt.title("DTW Cluster Visualization\n(colors = type, shapes = cluster)")
        plt.tight_layout()
        plt.show()
