#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mahimahi_validation.dashboard.aqm_validation_tool import AQMValidationTool


def run_mode_triplet(tool: AQMValidationTool, mode: str):
    tool.set_mode(mode)
    tool.run_parser()
    tool.view_histograms()


def run_one_pair(*, qdisc_dir: Path, mahi_dir: Path, out_dir: Path, bandwidth: str = "200Mbps"):
    tool = AQMValidationTool(
        qdisc_dir=qdisc_dir,
        mahimahi_dir=mahi_dir,
        out_dir=out_dir,
        dtw_mode="packets",
        bandwith=bandwidth,
    )

    print("===================================================")
    print("Headless Validation Runner")
    print(f"qdisc_dir  : {qdisc_dir.resolve()}")
    print(f"mahi_dir   : {mahi_dir.resolve()}")
    print(f"out_dir    : {out_dir.resolve()}")
    print("===================================================")

    # 1) PACKETS: parser -> hist -> overlay
    run_mode_triplet(tool, "packets")

    # 2) IPERF totalreceived: histogram-only
    tool.set_mode_iperf_totalreceived()
    tool.view_histograms()

    # 3) ECN MARK: parser -> hist -> overlay
    run_mode_triplet(tool, "ecn_mark")

    # 4) PACKET DROPPED TOTAL: parser -> hist -> overlay
    run_mode_triplet(tool, "packet_dropped_total")

    # # 5) L_QUEUE_DELAY: parser -> hist -> overlay
    # run_mode_triplet(tool, "qdelay_l_ms")

    # # 6) C_QUEUE_DELAY: parser -> hist -> overlay
    # run_mode_triplet(tool, "qdelay_c_ms")

    # # 7) L_AVERAGE_QUEUE_DELAY: histogram-only
    # tool.set_mode_l_average_queue_delay()
    # tool.view_histograms()

    # # 8) C_AVERAGE_QUEUE_DELAY: histogram-only
    # tool.set_mode_c_average_queue_delay()
    # tool.view_histograms()

    print("\nDONE. Outputs in:", out_dir.resolve())


def main():
    here = Path(__file__).resolve().parent   # dashboard/
    root = here.parent                       # repo_root/

    # ----------------------------------------------------------------
    # Put your (qdisc_dir, mahi_dir, name) pairs here.
    # `name` is used to create a subfolder under figs_headless/
    # ----------------------------------------------------------------
    PAIRS = [
        # ---- placeholders (edit these) ----
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

    base_out = root / "figs_headless"

    for spec in PAIRS:
        name = spec["name"]
        qdisc_dir = Path(spec["qdisc"])
        mahi_dir = Path(spec["mahi"])
        out_dir = base_out / name
        bandwidth = spec.get("bandwidth", "200Mbps")

        # Basic existence check (skip if missing, but don’t crash the whole run)
        if not qdisc_dir.exists() or not mahi_dir.exists():
            print("---------------------------------------------------")
            print(f"SKIP {name}: missing paths")
            print(f"  qdisc: {qdisc_dir}")
            print(f"  mahi : {mahi_dir}")
            print("---------------------------------------------------")
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        run_one_pair(qdisc_dir=qdisc_dir, mahi_dir=mahi_dir, out_dir=out_dir, bandwidth=bandwidth)


if __name__ == "__main__":
    main()