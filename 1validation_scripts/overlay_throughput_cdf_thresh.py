#!/usr/bin/env python3
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mahimahi_validation.dashboard.overlay_plots import Overlay_Plot


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
            "thresh_1": root / "mahimahi" / "12mbps" / "l4s",
            "thresh_5": root / "iperf_max_delay_mahimahi" / "12mbps" / "l4s-5",
            "thresh_10": root / "iperf_max_delay_mahimahi" / "12mbps" / "l4s-10",
            "kernel": root / "qdisc" / "12mbps" / "l4s",
            "bandwidth": "12mbps",
        },

        # ===================== 50mbps =====================
        {
            "name": "50mbps_throughput",
            "variable": "iperf",
            "thresh_1": root / "mahimahi" / "50mbps" / "l4s",
            "thresh_5": root / "iperf_max_delay_mahimahi" / "50mbps-5" / "l4s",
            "thresh_10": root / "iperf_max_delay_mahimahi" / "50mbps-10" / "l4s",
            "kernel": root / "qdisc" / "50mbps" / "l4s",
            "bandwidth": "50mbps",
        },

        # ===================== 200mbps =====================
        {
            "name": "200mbps_throughput",
            "variable": "iperf",
            "thresh_1": root / "mahimahi" / "200mbps" / "l4s",
            "thresh_5": root / "iperf_max_delay_mahimahi" / "200mbps-5" / "l4s",
            "thresh_10": root / "iperf_max_delay_mahimahi" / "200mbps-10" / "l4s",
            "kernel": root / "qdisc" / "200mbps" / "l4s",
            "bandwidth": "200mbps",
        },
    ]

    first = True
    for spec in PAIRS:

        plotter = Overlay_Plot(
            variable=spec["variable"],
            thresh_1_root=spec["thresh_1"],
            thresh_5_root=spec["thresh_5"],
            thresh_10_root=spec["thresh_10"],
            kernel_root=spec["kernel"],
            bdp=spec["bandwidth"],
        )

        # Only CDF, only SVG
        out_svg = base_out / f"{spec['name']}_cdf.svg"
        if (first): 
            plotter.plot_cdf(out_path=out_svg, ccdf=False, show_y_axis=True)
            first = False
        else:
            plotter.plot_cdf(out_path=out_svg, ccdf=False, show_y_axis=False)
        print("Generated:", out_svg)

    print("DONE:", base_out.resolve())


if __name__ == "__main__":
    main()