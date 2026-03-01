# validation_steps/trace_parsers.py
from __future__ import annotations

import re

# ============================================================
# Worker-side parsers (log -> trace dict)
# ============================================================
_TICK_NS_RE = re.compile(r"^(?:TICK_NS|TS_NS)\s+(\d+)\s*$")

_DELAY_RE = re.compile(
    r"\bdelay_c\s+(\d+)([a-zA-Z]+)\s+delay_l\s+(\d+)([a-zA-Z]+)\b"
)


def _parse_int_prefix(s: str, default: int = 0) -> int:
    s = s.strip()
    if not s:
        return default

    i = 0
    sign = 1
    if s[0] == "-":
        sign = -1
        i = 1

    num = 0
    start = i
    while i < len(s) and s[i].isdigit():
        num = num * 10 + (ord(s[i]) - 48)
        i += 1

    if i == start:
        return default
    return sign * num


def _parse_tc_num(s: str) -> int:
    s = s.strip()
    if not s:
        return 0

    mult = 1
    last = s[-1]
    if last in ("K", "M", "G", "T"):
        if last == "K":
            mult = 1_000
        elif last == "M":
            mult = 1_000_000
        elif last == "G":
            mult = 1_000_000_000
        elif last == "T":
            mult = 1_000_000_000_000
        s = s[:-1]

    return int(float(s) * mult)


def unit_to_ms(v: int, unit: str) -> float:
    u = unit.strip().lower()
    if u in ("us", "usec", "usecs"):
        return v / 1000.0
    if u in ("ms", "msec", "msecs"):
        return float(v)
    if u in ("ns", "nsec", "nsecs"):
        return v / 1e6
    return v / 1000.0


def read_mahi_trace_full_worker(path_str: str) -> dict:
    t_ms = []
    q_pkts = []
    q_bytes = []
    ecn_mark = []

    qdelay_l_ms = []
    qdelay_c_ms = []

    drop_total = []

    drop_l4s = []
    drop_classic = []
    drop_overflow = []
    drop_overload = []
    drop_not_ect = []

    with open(path_str, "r", errors="ignore") as f:
        for line in f:
            if not line.startswith("[QUEUE_STATS]"):
                continue

            parts = line.split()

            tm = None
            qp = qb = em = None

            qdl = None
            qdc = None

            dtot = None
            dl4s = dcl = dovf = dovl = dnet = 0

            for tok in parts:
                if tok.startswith("t_ms="):
                    v = tok[5:].strip().rstrip(",")
                    try:
                        tm = float(v)
                    except Exception:
                        tm = None

                elif tok.startswith("q_pkts="):
                    qp = _parse_int_prefix(tok[7:], default=None)
                elif tok.startswith("q_bytes="):
                    qb = _parse_int_prefix(tok[8:], default=None)
                elif tok.startswith("ecn_mark="):
                    em = _parse_int_prefix(tok[9:], default=None)

                elif tok.startswith("qdelay_l_ms="):
                    v = tok[len("qdelay_l_ms="):].strip().rstrip(",")
                    try:
                        qdl = float(v)
                    except Exception:
                        qdl = None
                elif tok.startswith("qdelay_c_ms="):
                    v = tok[len("qdelay_c_ms="):].strip().rstrip(",")
                    try:
                        qdc = float(v)
                    except Exception:
                        qdc = None

                elif tok.startswith("drop="):
                    dtot = _parse_int_prefix(tok[5:], default=0)

                elif tok.startswith("drop_l4s="):
                    dl4s = _parse_int_prefix(tok[9:], default=0)
                elif tok.startswith("drop_classic="):
                    dcl = _parse_int_prefix(tok[13:], default=0)
                elif tok.startswith("drop_overflow="):
                    dovf = _parse_int_prefix(tok[14:], default=0)
                elif tok.startswith("drop_overload="):
                    dovl = _parse_int_prefix(tok[14:], default=0)
                elif tok.startswith("drop_not_ect="):
                    dnet = _parse_int_prefix(tok[13:], default=0)

            if tm is None or qp is None or qb is None or em is None:
                continue

            t_ms.append(tm)
            q_pkts.append(qp)
            q_bytes.append(qb)
            ecn_mark.append(em)

            qdelay_l_ms.append(0.0 if qdl is None else float(qdl))
            qdelay_c_ms.append(0.0 if qdc is None else float(qdc))

            if dtot is None:
                dtot = dl4s + dcl + dovf + dovl + dnet
            drop_total.append(int(dtot))

            drop_l4s.append(dl4s)
            drop_classic.append(dcl)
            drop_overflow.append(dovf)
            drop_overload.append(dovl)
            drop_not_ect.append(dnet)

    return {
        "t_ms": t_ms,
        "q_pkts": q_pkts,
        "q_bytes": q_bytes,
        "ecn_mark": ecn_mark,
        "qdelay_l_ms": qdelay_l_ms,
        "qdelay_c_ms": qdelay_c_ms,
        "drop_total": drop_total,
        "drop_l4s": drop_l4s,
        "drop_classic": drop_classic,
        "drop_overflow": drop_overflow,
        "drop_overload": drop_overload,
        "drop_not_ect": drop_not_ect,
    }


def read_qdisc_trace_full_worker(path_str: str) -> dict:
    tr = {
        "t_ms": [],
        "q_pkts": [],
        "q_bytes": [],
        "ecn_mark": [],
        "drop_total": [],
        "qdelay_c_ms": [],
        "qdelay_l_ms": [],
    }

    cur_ts = None
    t0 = None

    last_bytes = None
    last_pkts = None
    block_ecn = 0
    block_drop_total = None
    block_qdelay_c_ms = None
    block_qdelay_l_ms = None

    def flush():
        nonlocal cur_ts, t0, last_bytes, last_pkts
        nonlocal block_ecn, block_drop_total, block_qdelay_c_ms, block_qdelay_l_ms

        if cur_ts is None or last_pkts is None or last_bytes is None:
            return
        if t0 is None:
            t0 = cur_ts

        tr["t_ms"].append((cur_ts - t0) / 1e6)
        tr["q_pkts"].append(last_pkts)
        tr["q_bytes"].append(last_bytes)
        tr["ecn_mark"].append(block_ecn)
        tr["drop_total"].append(0 if block_drop_total is None else block_drop_total)
        tr["qdelay_c_ms"].append(0.0 if block_qdelay_c_ms is None else float(block_qdelay_c_ms))
        tr["qdelay_l_ms"].append(0.0 if block_qdelay_l_ms is None else float(block_qdelay_l_ms))

    with open(path_str, "r", errors="ignore") as f:
        for line in f:
            m = _TICK_NS_RE.match(line)
            if m:
                flush()
                cur_ts = int(m.group(1))
                last_bytes = None
                last_pkts = None
                block_ecn = 0
                block_drop_total = None
                block_qdelay_c_ms = None
                block_qdelay_l_ms = None
                continue

            if "backlog " in line:
                parts = line.split()
                for k, tok in enumerate(parts):
                    if tok == "backlog" and k + 2 < len(parts):
                        b = parts[k + 1]
                        p = parts[k + 2]
                        if b.endswith("b") and p.endswith("p"):
                            b = b[:-1]
                            p = p[:-1]
                            last_bytes = _parse_tc_num(b)
                            last_pkts = _parse_tc_num(p)
                        break
                continue

            if "ecn_mark" in line:
                parts = line.split()
                for k, tok in enumerate(parts):
                    if tok == "ecn_mark" and k + 1 < len(parts):
                        block_ecn = int(parts[k + 1])
                        break

            if "delay_c" in line and "delay_l" in line:
                mm = _DELAY_RE.search(line)
                if mm:
                    c_val = int(mm.group(1))
                    c_unit = mm.group(2)
                    l_val = int(mm.group(3))
                    l_unit = mm.group(4)
                    block_qdelay_c_ms = unit_to_ms(c_val, c_unit)
                    block_qdelay_l_ms = unit_to_ms(l_val, l_unit)

            if "dropped" in line:
                idx = line.find("dropped")
                if idx != -1:
                    rest = line[idx + len("dropped"):].lstrip()
                    num = 0
                    sign = 1
                    i = 0
                    if i < len(rest) and rest[i] == "-":
                        sign = -1
                        i += 1
                    while i < len(rest) and rest[i].isdigit():
                        num = num * 10 + (ord(rest[i]) - 48)
                        i += 1
                    block_drop_total = sign * num

    flush()
    return tr