#!/usr/bin/env python3
import os
import re
import shutil
import sys
from collections import defaultdict

def parse_id(fname: str):
    """
    Extract run id from filenames like:
      iperf3_0.classic.client.log
      iperf3_0.14s.client.log
    Rule: take the segment before the first '.', and read trailing digits.
    """
    if "." not in fname:
        return None
    left, rest = fname.split(".", 1)  # left="iperf3_0", rest="classic.client.log"
    m = re.search(r"(\d+)$", left)
    if not m:
        return None
    run_id = int(m.group(1))
    left_prefix = left[: -len(m.group(1))]  # "iperf3_"
    return run_id, left_prefix, rest

def collect_ids(folder: str):
    ids = set()
    for fn in os.listdir(folder):
        p = parse_id(fn)
        if p is not None:
            ids.add(p[0])
    return ids

def group_by_id(folder: str):
    groups = defaultdict(list)  # id -> [filenames]
    for fn in os.listdir(folder):
        p = parse_id(fn)
        if p is None:
            continue
        run_id, _, _ = p
        groups[run_id].append(fn)
    return groups

def next_free_id(used: set, start: int = 0):
    x = start
    while x in used:
        x += 1
    return x

def merge_fill_gaps(dst_folder: str, src_folder: str, move=True):
    used_ids = collect_ids(dst_folder)
    src_groups = group_by_id(src_folder)

    # Assign each src run_id -> a free dst id (fill gaps starting at 0)
    mapping = {}
    cursor = 0
    for old_id in sorted(src_groups.keys()):
        new_id = next_free_id(used_ids, cursor)
        mapping[old_id] = new_id
        used_ids.add(new_id)
        cursor = new_id + 1  # keep scanning forward, but still fills earlier gaps first

    op = shutil.move if move else shutil.copy2

    for old_id, files in src_groups.items():
        new_id = mapping[old_id]
        for fn in files:
            run_id, left_prefix, rest = parse_id(fn)
            assert run_id == old_id
            new_fn = f"{left_prefix}{new_id}.{rest}"

            src_path = os.path.join(src_folder, fn)
            dst_path = os.path.join(dst_folder, new_fn)

            if os.path.exists(dst_path):
                raise RuntimeError(f"Destination exists already: {dst_path}")

            op(src_path, dst_path)
            print(f"{fn} -> {new_fn}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: merge_fill_gaps.py <dst_folder> <src_folder> [--copy]")
        sys.exit(1)

    dst = sys.argv[1]
    src = sys.argv[2]
    do_move = "--copy" not in sys.argv[3:]
    merge_fill_gaps(dst, src, move=do_move)