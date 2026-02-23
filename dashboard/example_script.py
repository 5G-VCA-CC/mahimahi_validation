#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

# If your class is in another file/module, update this import.
from aqm_validation_tool import AQMValidationTool


def run_mode_triplet(tool: AQMValidationTool, mode: str):
    tool.set_mode(mode)
    tool.run_parser()
    tool.view_histograms()
    tool.plot_graph_check()


def main():
    # ------------------------------------------------------------
    # Layout:
    #   repo_root/
    #     50mbps/
    #     50mbps-5/l4s/
    #     dashboard/
    #       example_script.py   <-- this file
    #
    # So from dashboard/, go up one level to repo_root/.
    # ------------------------------------------------------------
    here = Path(__file__).resolve().parent          # dashboard/
    root = here.parent                              # repo_root/

    qdisc_dir = root / "50mbps" / "l4s"
    mahi_dir  = root / "iperf_max_delay_mahimahi"/ "50mbps-5" / "l4s"
    out_dir   = root / "figs_headless"

    tool = AQMValidationTool(
        qdisc_dir=qdisc_dir,
        mahimahi_dir=mahi_dir,
        out_dir=out_dir,
        dtw_mode="packets",
    )

    print("===================================================")
    print("Headless Validation Runner")
    print(f"script_dir : {here}")
    print(f"repo_root  : {root}")
    print(f"qdisc_dir  : {Path(tool.qdisc_dir).resolve()}")
    print(f"mahi_dir   : {Path(tool.mahimahi_dir).resolve()}")
    print(f"out_dir    : {tool.out_dir.resolve()}")
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

    # 5) L_QUEUE_DELAY: parser -> hist -> overlay
    run_mode_triplet(tool, "qdelay_l_ms")

    # 6) C_QUEUE_DELAY: parser -> hist -> overlay
    run_mode_triplet(tool, "qdelay_c_ms")

    # 7) L_AVERAGE_QUEUE_DELAY: histogram-only
    tool.set_mode_l_average_queue_delay()
    tool.view_histograms()

    # 8) C_AVERAGE_QUEUE_DELAY: histogram-only
    tool.set_mode_c_average_queue_delay()
    tool.view_histograms()

    print("\nDONE. All outputs are in:", tool.out_dir.resolve())


if __name__ == "__main__":
    main()