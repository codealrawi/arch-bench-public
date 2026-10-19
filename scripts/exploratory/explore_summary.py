"""ПОИСКОВЫЙ анализ: сводка explore_ops.csv (состав успешных операций, фазы соединения k6).

python scripts/exploratory/explore_summary.py explore_ops.csv
"""

import sys

import pandas as pd

COLS = ["n", "n_ok", "sum_dur_ms", "sum_blocked_ms", "sum_connecting_ms"]
PROFILE = (
    "feed .30 get_post .15 search .10 stats .10 create_post .12 create_comment .10 "
    "reaction .06 get_profile .05 update_profile .02"
)
T = 300.0

d = pd.read_csv(sys.argv[1])
g = d.groupby(["arch", "vus", "op"])[COLS].sum().reset_index()
runs = d.groupby(["arch", "vus"])["run"].nunique()
g["runs"] = g.apply(lambda r: runs[(r.arch, r.vus)], axis=1)
g["ok_per_s"] = g.n_ok / (T * g.runs)
g["ok_share"] = g.n_ok / g.n

pd.set_option("display.width", 220)
print("=== Успешные запросы/с по операциям (среднее по прогонам) ===")
print(g.pivot_table(index=["vus", "op"], columns="arch", values="ok_per_s").round(1).to_string())
print("\n=== Доля успешных среди завершённых, по операциям ===")
high = g[g.vus >= 1000]
print(high.pivot_table(index=["vus", "op"], columns="arch", values="ok_share").round(2).to_string())
tot = g.groupby(["arch", "vus"])[COLS].sum()
tot["mean_dur_s"] = tot.sum_dur_ms / tot.n / 1000
tot["mean_blocked_s"] = tot.sum_blocked_ms / tot.n / 1000
tot["mean_connecting_s"] = tot.sum_connecting_ms / tot.n / 1000
print("\n=== Фазы соединения k6 (не входят в http_req_duration), среднее на запрос ===")
print(tot[["mean_dur_s", "mean_blocked_s", "mean_connecting_s"]].round(3).to_string())
mix = g.pivot_table(index=["arch", "vus"], columns="op", values="n_ok", aggfunc="sum")
mix = mix.div(mix.sum(axis=1), axis=0)
print(f"\n=== Состав УСПЕШНЫХ ответов (доли; заданный профиль: {PROFILE}) ===")
print(mix.round(3).to_string())
