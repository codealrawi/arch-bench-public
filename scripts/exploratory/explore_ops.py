"""ПОИСКОВЫЙ анализ (не из плана §0–§10): состав завершённых операций и фазы соединения k6.

Только чтение experiments/raw. Для каждой (архитектура, прогон, ступень), запросы фазы measure:
по операции — завершённые, успешные (2xx/3xx), сумма длительностей http_req_duration;
по фазам k6 — суммы http_req_blocked и http_req_connecting (не входят в http_req_duration).
Вывод: /root/explore_ops.csv (строка на архитектуру × прогон × ступень × операция).
"""

import csv
import glob
import gzip
import json
import sys
from collections import defaultdict

STEPS = {"0100", "1000", "2000", "5000"}
out = open("/root/explore_ops.csv", "w", newline="")  # noqa: SIM115 — пишется построчно
w = csv.writer(out)
w.writerow(
    ["arch", "run", "vus", "op", "n", "n_ok", "sum_dur_ms", "sum_blocked_ms", "sum_connecting_ms"]
)
files = sorted(glob.glob("/root/arch-bench/experiments/raw/*/run_*/step_*/points.json.gz"))
for i, f in enumerate(files):
    parts = f.split("/")
    arch, run, step = parts[-4], parts[-3], parts[-2].split("_")[1]
    if step not in STEPS:
        continue
    agg = defaultdict(lambda: [0, 0, 0.0, 0.0, 0.0])
    with gzip.open(f, "rt") as fh:
        for line in fh:
            if '"Point"' not in line or '"measure"' not in line:
                continue
            o = json.loads(line)
            m = o["metric"]
            if m not in ("http_req_duration", "http_req_blocked", "http_req_connecting"):
                continue
            t = o["data"]["tags"]
            if t.get("phase") != "measure":
                continue
            a = agg[t.get("op", "?")]
            v = o["data"]["value"]
            if m == "http_req_duration":
                a[0] += 1
                s = int(t.get("status") or 0)
                if 200 <= s <= 399:
                    a[1] += 1
                a[2] += v
            elif m == "http_req_blocked":
                a[3] += v
            else:
                a[4] += v
    for op, a in sorted(agg.items()):
        w.writerow([arch, run, int(step), op, *a])
    out.flush()
    print(f"{i + 1}/{len(files)} {arch} {run} {step}", file=sys.stderr, flush=True)
out.close()
print("done", file=sys.stderr)
