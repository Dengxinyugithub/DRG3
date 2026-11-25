# -*- coding: utf-8 -*-
"""接口吞吐测试：构造 N 条随机病例，走 /api/v1/group/batch。

用法：python tools/bench.py [--n 2000] [--url http://127.0.0.1:8765] [--chunk 1000]
"""

import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from grouper import load_config  # noqa: E402

DIAG_SETS = ["DI_B00", "DI_E00", "DI_G00", "DI_I00", "DI_K00", "DI_L00", "DI_F00"]
OP_SETS = ["OP_BE1", "OP_EB1", "OP_GE1", "OP_IC3", "OP_MB1"]


def build(n: int, seed: int = 7):
    cfg = load_config()
    rnd = random.Random(seed)
    diags = [(name, sorted(cfg.sets[name])) for name in DIAG_SETS if name in cfg.sets]
    ops = [(name, sorted(cfg.sets[name])) for name in OP_SETS if name in cfg.sets]
    mccs = [r["code"] for r in cfg.cc_by_code.values() if r["type"] == "MCC"][:200]
    ccs = [r["code"] for r in cfg.cc_by_code.values() if r["type"] == "CC"][:200]
    records = []
    for i in range(n):
        name, pool = rnd.choice(diags)
        rec = {
            "patient_id": f"BENCH{i:06d}",
            "age": rnd.choice([3, 25, 45, 65, 80]),
            "age_unit": "years",
            "gender": rnd.choice(["M", "F"]),
            "primary_diagnosis": {"code": rnd.choice(pool)},
            "other_diagnoses": [],
        }
        if rnd.random() < 0.5:
            rec["other_diagnoses"].append({"code": rnd.choice(mccs if rnd.random() < 0.4 else ccs)})
        if rnd.random() < 0.7:
            oname, opool = rnd.choice(ops)
            rec["primary_operation"] = {"code": rnd.choice(opool)}
        records.append(rec)
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=2000)
    parser.add_argument("--chunk", type=int, default=1000)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    records = build(args.n)
    grouped = total = 0
    wall = 0.0
    server_ms = 0.0
    for start in range(0, len(records), args.chunk):
        chunk = records[start:start + args.chunk]
        body = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{args.url}/api/v1/group/batch",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        wall += time.perf_counter() - t0
        summary = data["summary"]
        server_ms += summary["elapsed_ms"]
        grouped += summary["grouped"]
        total += summary["total"]
    print(f"条数={total}  入组={grouped}  未入组={total - grouped}")
    print(f"客户端总耗时={wall * 1000:.0f} ms  平均 {wall * 1000 / max(total, 1):.2f} ms/条  吞吐 {total / wall:.0f} 条/秒")
    print(f"服务端分组耗时合计={server_ms:.0f} ms  平均 {server_ms / max(total, 1):.3f} ms/条")


if __name__ == "__main__":
    main()
