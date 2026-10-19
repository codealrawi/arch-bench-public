"""ТОЛЬКО ЧТЕНИЕ: готовые реплики по Deployment в окне измерения (pods.csv), среднее по прогонам."""
import csv
import glob
import json
from collections import defaultdict

acc = defaultdict(list)  # (arch, vus, ns/deploy) -> [mean ready per run]
mx = defaultdict(int)
for arch in ("microservices", "serverless"):
    for sd in glob.glob(f"/root/arch-bench/experiments/raw/{arch}/run_*/step_*"):
        sm = json.load(open(f"{sd}/step_meta.json"))
        t0 = sm["k6_started_at"] + 90  # ramp 30 + warmup 60
        t1 = t0 + 300
        per = defaultdict(list)
        for r in csv.DictReader(open(f"{sd}/pods.csv")):
            t = float(r["t"])
            if t0 <= t <= t1:
                per[(r["namespace"], r["deployment"])].append(int(r["ready_replicas"]))
        for k, v in per.items():
            if k[0] in ("arch-bench", "openfaas-fn", "openfaas") and v:
                key = (arch, sm["vus"], f"{k[0]}/{k[1]}")
                acc[key].append(sum(v) / len(v))
                mx[key] = max(mx[key], max(v))
print("arch,vus,deployment,mean_ready,max_ready")
for k in sorted(acc):
    if k[2].split("/")[1] in ("postgres", "nats", "queue-worker", "prometheus", "alertmanager"):
        continue
    v = acc[k]
    print(f"{k[0]},{k[1]},{k[2]},{sum(v)/len(v):.2f},{mx[k]}")
