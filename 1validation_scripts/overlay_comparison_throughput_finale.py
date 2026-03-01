#!/usr/bin/env python3
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mahimahi_validation.dashboard.overlay_plots_target import Overlay_Plot_BDP


def main():
    here = Path(__file__).resolve().parent
    root = here.parent

    base_out = root / "figs_overlay"
    base_out.mkdir(parents=True, exist_ok=True)

    spec = {
        "name": "bdp_throughput",
        "variable": "iperf",

        # Mahimahi
        "bdp_12":  root / "mahimahi-FINALE" / "12mbps"  / "dual-5-30",
        "bdp_50":  root / "mahimahi-FINALE" / "50mbps"  / "dual-5-30",
        "bdp_200": root / "mahimahi-FINALE" / "200mbps" / "dual-10-45",

        # Kernel (qdisc)
        "kernel_bdp_12":  root / "qdisc" / "12mbps"  / "dual",
        "kernel_bdp_50":  root / "qdisc" / "50mbps"  / "dual",
        "kernel_bdp_200": root / "qdisc" / "200mbps" / "dual",
    }

    plotter = Overlay_Plot_BDP(
        variable=spec["variable"],
        bdp_12_root=spec["bdp_12"],
        bdp_50_root=spec["bdp_50"],
        bdp_200_root=spec["bdp_200"],
        kernel_bdp_12_root=spec["kernel_bdp_12"],
        kernel_bdp_50_root=spec["kernel_bdp_50"],
        kernel_bdp_200_root=spec["kernel_bdp_200"],
        bdp="",
    )

    # Base output name (function will write _bdp12/_bdp50/_bdp200)
    out_base = base_out / f"{spec['name']}_cdf.svg"

    figs, axs = plotter.plot_cdf_3panel(out_path=out_base)

    # Print the actual files that should exist
    generated = [
        out_base.with_name(f"{out_base.stem}_bdp12{out_base.suffix}"),
        out_base.with_name(f"{out_base.stem}_bdp50{out_base.suffix}"),
        out_base.with_name(f"{out_base.stem}_bdp200{out_base.suffix}"),
    ]
    for p in generated:
        print("Generated:", p)

    print("DONE:", base_out.resolve())


if __name__ == "__main__":
    main()