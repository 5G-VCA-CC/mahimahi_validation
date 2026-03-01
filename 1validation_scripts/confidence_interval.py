#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mahimahi_validation.dashboard.aqm_validation_tool import AQMValidationTool


def run_one_pair_ci_only(
    *,
    qdisc_dir: Path,
    mahi_dir: Path,
    out_dir: Path,
    bandwidth: str,
    ns_start: int = 10,
    ns_stop: int = 100,
    ns_step: int = 5,
    quantile: float = 95.0,
    B: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
):
    tool = AQMValidationTool(
        qdisc_dir=qdisc_dir,
        mahimahi_dir=mahi_dir,
        out_dir=out_dir,
        dtw_mode="ecn_mark",     # only ECN
        bandwidth=bandwidth,     # <-- NOTE: was misspelled in your older runner
    )

    print("===================================================")
    print("Headless CI Runner (ECN only)")
    print(f"qdisc_dir  : {qdisc_dir.resolve()}")
    print(f"mahi_dir   : {mahi_dir.resolve()}")
    print(f"out_dir    : {out_dir.resolve()}")
    print(f"bandwidth  : {bandwidth}")
    print(f"ns         : {ns_start}..{ns_stop} step {ns_step}")
    print(f"quantile   : {quantile}")
    print(f"B          : {B}")
    print(f"alpha      : {alpha}")
    print(f"seed       : {seed}")
    print("===================================================")

    # (re)build cache for ECN, then plot CI vs n
    tool.set_mode("ecn_mark")
    tool.run_parser()

    ns = tuple(range(ns_start, ns_stop + 1, ns_step))
    tool.plot_ci_width_vs_n(
        ns=ns,
        quantile=quantile,
        B=B,
        alpha=alpha,
        seed=seed,
    )

    # optional: print the table too (remove if you truly want ONLY the plot)
    tool.print_ci_vs_n(
        ns=ns,
        quantile=quantile,
        B=B,
        alpha=alpha,
        seed=seed,
    )

    print("\nDONE. Outputs in:", out_dir.resolve())


def main():
    here = Path(__file__).resolve().parent
    root = here.parent

    PAIRS = [
        {
            "name": "dual_12mbps_finale",
            "qdisc": root / "qdisc" / "12mbps" / "dual",
            "mahi":  root / "mahimahi-FINALE" / "12mbps" / "dual-5-30",
            "bandwidth": "12Mbps",
        },
        {
            "name": "dual_50mbps_finale",
            "qdisc": root / "qdisc" / "50mbps" / "dual",
            "mahi":  root / "mahimahi-FINALE" / "50mbps" / "dual-5-30",
            "bandwidth": "50Mbps",
        },
        {
            "name": "dual_200mbps_finale",
            "qdisc": root / "qdisc" / "200mbps" / "dual",
            "mahi":  root / "mahimahi-FINALE" / "200mbps" / "dual-10-45",
            "bandwidth": "200Mbps",
        },
    ]

    base_out = root / "figs_headless_ci_ecn"

    for spec in PAIRS:
        name = spec["name"]
        qdisc_dir = Path(spec["qdisc"])
        mahi_dir = Path(spec["mahi"])
        out_dir = base_out / name
        bandwidth = spec.get("bandwidth", "200Mbps")

        if not qdisc_dir.exists() or not mahi_dir.exists():
            print("---------------------------------------------------")
            print(f"SKIP {name}: missing paths")
            print(f"  qdisc: {qdisc_dir}")
            print(f"  mahi : {mahi_dir}")
            print("---------------------------------------------------")
            continue

        out_dir.mkdir(parents=True, exist_ok=True)

        run_one_pair_ci_only(
            qdisc_dir=qdisc_dir,
            mahi_dir=mahi_dir,
            out_dir=out_dir,
            bandwidth=bandwidth,
            ns_start=10,
            ns_stop=100,
            ns_step=5,     # change to 10 if you want fewer points
            quantile=95.0,
            B=2000,
            alpha=0.05,
            seed=0,
        )


if __name__ == "__main__":
    main()