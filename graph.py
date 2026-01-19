import re
import matplotlib.pyplot as plt

DT_MS = 16
CUTOFF_MS = 1000
CUTOFF_SAMPLES = CUTOFF_MS // DT_MS  # 62 samples (≈ 1000ms)

# Matches: backlog 2484b 2p requeues 0
BACKLOG_RE = re.compile(r"\bbacklog\s+(\d+)b\s+(\d+)p\b")

def read_backlog_packets(path: str):
    """
    Returns a list of queue backlog in PACKETS (the 'Xp' field).
    Tries to only count the dualpi2 backlog by taking the second backlog in each block
    (first is htb, second is dualpi2). If only one exists, it uses that one.
    """
    pkts = []
    current_block_vals = []

    with open(path, "r", errors="ignore") as f:
        for line in f:
            if line.startswith("------ "):  # new sample block
                if current_block_vals:
                    pkts.append(
                        current_block_vals[1]
                        if len(current_block_vals) >= 2
                        else current_block_vals[0]
                    )
                current_block_vals = []
                continue

            m = BACKLOG_RE.search(line)
            if m:
                current_block_vals.append(int(m.group(2)))

    # flush last block
    if current_block_vals:
        pkts.append(
            current_block_vals[1]
            if len(current_block_vals) >= 2
            else current_block_vals[0]
        )

    return pkts

if __name__ == "__main__":
    path = input("Path to log file: ").strip()
    q_pkts = read_backlog_packets(path)

    if not q_pkts:
        raise SystemExit("No 'backlog ...b ...p' lines found.")

    # ---- drop first 1000ms ----
    q_pkts = q_pkts[CUTOFF_SAMPLES:]

    t_ms = [(i + CUTOFF_SAMPLES) * DT_MS for i in range(len(q_pkts))]

    print(f"Plotted {len(q_pkts)} samples (after first {CUTOFF_MS} ms removed).")

    plt.figure()
    plt.plot(
        t_ms,
        q_pkts,
        linewidth=0.6,     # thinner line
        marker="o",        # show all points
        markersize=2
    )
    plt.xlabel("Time (ms)")
    plt.ylabel("Queue backlog (packets)")
    plt.title("dualpi2 queue backlog vs time (16 ms sampling)")
    plt.tight_layout()
    plt.show()
