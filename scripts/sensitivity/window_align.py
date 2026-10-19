"""ТОЛЬКО ЧТЕНИЕ: сдвиг окна CPU (Prometheus) относительно окна измерения k6.

Окно k6 measure: [S + 90, S + 390], S — момент старта сценария (метрика arch_bench_scenario_start_ms).
Окно CPU (run.py): [F − 30 − 300, F − 30], F — k6_finished_at из step_meta.json.
Сдвиг = (F − 30 − 300) − (S + 90), с.
"""
import glob
import gzip
import json

print("arch,run,vus,offset_s,setup_s")
for sd in sorted(glob.glob("/root/arch-bench/experiments/raw/*/run_*/step_*")):
    if "_invalid" in sd:
        continue
    sm = json.load(open(f"{sd}/step_meta.json"))
    s = None
    with gzip.open(f"{sd}/points.json.gz", "rt") as f:
        for line in f:
            if "arch_bench_scenario_start_ms" in line:
                o = json.loads(line)
                if o.get("type") == "Point":
                    s = o["data"]["value"] / 1000.0
                    break
    if s is None:
        continue
    fin = sm["k6_finished_at"]
    off = (fin - 330) - (s + 90)
    p = sd.split("/")
    print(f"{p[-3] and p[-4]},{p[-3]},{sm['vus']},{off:.2f},{s - sm['k6_started_at']:.2f}")
