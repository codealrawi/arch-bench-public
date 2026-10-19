"""ПРОВЕРКА ЧУВСТВИТЕЛЬНОСТИ (только чтение raw и запросы к Prometheus).

Пересчёт CPU по группам за ТОЧНОЕ окно измерения k6 [S+90, S+390] (S — старт сценария из
points.json.gz) теми же запросами и той же группировкой, что experiments/orchestrator/resources.py.
Результат — /root/cpu_exact_window.csv; experiments/raw не изменяется.
"""
import glob
import gzip
import json
import sys

sys.path.insert(0, "/root/arch-bench")
from experiments.orchestrator import resources as R  # noqa: E402

PROM = "http://127.0.0.1:9090"
W = 300
out = open("/root/cpu_exact_window.csv", "w")
out.write("arch,run,vus,start_s,cpu_database,cpu_application,cpu_gateway,cpu_infrastructure,n_series\n")
for sd in sorted(glob.glob("/root/arch-bench/experiments/raw/*/run_*/step_*")):
    p = sd.split("/")
    arch, run = p[-3], p[-2]
    arch, run = p[-4] if False else sd.split("/raw/")[1].split("/")[0], sd.split("/")[-2]
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
    t_end = s + 90 + W
    cpu = R._query(PROM, R.cpu_query(W), t_end)
    root = R._query(PROM, R.root_cpu_query(W), t_end)
    node = float(root[0]["value"][1]) if root else None
    rows = R.rows_from_result(arch, cpu, node, [])
    g = {k: 0.0 for k in ("database", "application", "gateway", "infrastructure")}
    for r in rows:
        g[r["group"]] += float(r["cpu_seconds"])
    out.write(f"{arch},{run},{sm['vus']},{s:.3f},{g['database']:.3f},{g['application']:.3f},"
              f"{g['gateway']:.3f},{g['infrastructure']:.3f},{len(cpu)}\n")
    out.flush()
out.close()
print("done")
