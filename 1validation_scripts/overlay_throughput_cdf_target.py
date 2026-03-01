#!/usr/bin/env python3
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mahimahi_validation.dashboard.overlay_plots_target import Overlay_Plot_Target

def main():
    here = Path(__file__).resolve().parent
    root = here.parent

    base_out = root / "figs_overlay"
    base_out.mkdir(parents=True, exist_ok=True)

    PAIRS = [

        # ===================== 12mbps =====================
        {
            "name": "12mbps_throughput",
            "variable": "iperf",
            "thresh_1": root / "mahimahi" / "12mbps" / "classic",
            "thresh_5": root / "iperf_target_classic_mahimahi" / "target-30ms" / "12mbps" / "classic",
            "thresh_10": root / "iperf_target_classic_mahimahi" / "target-45ms" / "12mbps" / "classic-45",
            "kernel": root / "qdisc" / "12mbps" / "classic",
            "bandwidth": "12mbps",
        },

        # ===================== 50mbps =====================
        {
            "name": "50mbps_throughput",
            "variable": "iperf",
            "thresh_1": root / "mahimahi" / "50mbps" / "classic",
            "thresh_5": root / "iperf_target_classic_mahimahi" / "target-30ms" / "50mbps" / "classic",
            "thresh_10": root / "iperf_target_classic_mahimahi" / "target-45ms" / "50mbps" / "classic-45",
            "kernel": root / "qdisc" / "50mbps" / "classic",
            "bandwidth": "50mbps",
        },

        # ===================== 200mbps =====================
        {
            "name": "200mbps_throughput",
            "variable": "iperf",
            "thresh_1": root / "mahimahi" / "200mbps" / "classic",
            "thresh_5": root / "iperf_target_classic_mahimahi" / "target-30ms" / "200mbps" / "classic",
            "thresh_10": root / "iperf_target_classic_mahimahi" / "target-45ms" / "200mbps" / "classic-45",
            "kernel": root / "qdisc" / "200mbps" / "classic",
            "bandwidth": "200mbps",
        },
    ]

    first = True
    for spec in PAIRS:

        plotter = Overlay_Plot_Target(
            variable=spec["variable"],
            target_1_root=spec["thresh_1"],
            target_30_root=spec["thresh_5"],
            target_45_root=spec["thresh_10"],
            kernel_root=spec["kernel"],
            bdp=spec["bandwidth"],
        )

        # Only CDF, only SVG
        out_svg = base_out / f"{spec['name']}_cdf.svg"
        if first:
            plotter.plot_cdf(out_path=out_svg, ccdf=False, show_y_axis=True)
            first = False
        else:
            plotter.plot_cdf(out_path=out_svg, ccdf=False, show_y_axis=False)

        print("Generated:", out_svg)

    print("DONE:", base_out.resolve())


if __name__ == "__main__":
    main()