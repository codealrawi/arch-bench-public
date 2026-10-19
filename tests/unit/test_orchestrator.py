"""Оркестратор: seed k6, окружение k6, классификация CPU, критерии невалидности (TEST DATA ONLY)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.config import BenchConfig, load_bench_config
from experiments.orchestrator import invalid, resources
from experiments.orchestrator.k6runner import k6_env, k6_seed
from tests.fixtures.k6_fixture import point, start_point, write_points


@pytest.fixture(scope="module")
def cfg():  # type: ignore[no-untyped-def]
    return load_bench_config()


def test_k6_seed_same_across_architectures_and_distinct_across_steps(cfg) -> None:  # type: ignore[no-untyped-def]
    seeds = {(r, s): k6_seed(cfg, r, s) for r in range(1, 11) for s in range(8)}
    assert len(set(seeds.values())) == len(seeds)  # уникальны по (прогон, ступень)
    assert k6_seed(cfg, 3, 2) == cfg.load.k6_seed_base + 3002


def test_k6_env_comes_from_config(cfg) -> None:  # type: ignore[no-untyped-def]
    import json

    env = k6_env(
        cfg, architecture="serverless", base_url="http://h:31112", vus=500, seed=7, texts_path="t"
    )
    assert json.loads(env["ROUTES_JSON"]) == {
        "auth": "/function/auth",
        "content": "/function/content",
        "stats": "/function/stats",
    }
    assert env["THINK_S"] == str(cfg.load.think_time_s)
    assert env["MEASURE_S"] == str(cfg.load.measure_s)
    assert sum(json.loads(env["MIX_JSON"]).values()) == pytest.approx(1.0)
    assert env["EMAIL_TEMPLATE"] == "user{user_id}@bench.example.com"
    mono = k6_env(
        cfg, architecture="monolith", base_url="http://h:8000", vus=10, seed=1, texts_path="t"
    )
    assert json.loads(mono["ROUTES_JSON"]) == {"auth": "", "content": "", "stats": ""}
    assert mono["MIX_JSON"] == env["MIX_JSON"]  # один профиль для всех архитектур


@pytest.mark.parametrize(
    ("arch", "labels", "group"),
    [
        ("monolith", {"name": "arch-bench-monolith-app-1"}, "application"),
        ("monolith", {"name": "arch-bench-monolith-postgres-1"}, "database"),
        ("three_tier", {"name": "arch-bench-three-tier-backend-1"}, "application"),
        ("three_tier", {"name": "arch-bench-three-tier-frontend-1"}, "gateway"),
        ("three_tier", {"name": "prometheus"}, "infrastructure"),
        (
            "microservices",
            {"namespace": "arch-bench", "pod": "content-7d9f8b6c5d-x2k4p"},
            "application",
        ),
        (
            "microservices",
            {"namespace": "arch-bench", "pod": "auth-5c6d7e8f9a-abcde"},
            "application",
        ),
        (
            "microservices",
            {"namespace": "arch-bench", "pod": "gateway-5c6d7e8f9a-abcde"},
            "gateway",
        ),
        (
            "microservices",
            {"namespace": "kube-system", "pod": "coredns-6799fbcd5-q7w8e"},
            "infrastructure",
        ),
        ("serverless", {"namespace": "openfaas", "pod": "gateway-6b7c8d9e0f-zzzzz"}, "gateway"),
        (
            "serverless",
            {"namespace": "openfaas-fn", "pod": "content-5f6a7b8c9d-aaaaa"},
            "application",
        ),
        ("serverless", {"namespace": "openfaas", "pod": "nats-0"}, "infrastructure"),
    ],
)
def test_cpu_grouping(arch: str, labels: dict[str, str], group: str) -> None:
    assert resources.group_of(arch, labels) == group


def test_infrastructure_is_node_minus_workload() -> None:
    result = [
        {"metric": {"name": "arch-bench-monolith-app-1"}, "value": [0, "120.0"]},
        {"metric": {"name": "arch-bench-monolith-postgres-1"}, "value": [0, "60.0"]},
        {"metric": {"name": "cadvisor"}, "value": [0, "5.0"]},
    ]
    mem = [{"metric": {"name": "arch-bench-monolith-app-1"}, "value": [0, "1048576"]}]
    rows = resources.rows_from_result("monolith", result, node_total=200.0, mem=mem)
    by = {r["group"]: r["cpu_seconds"] for r in rows}
    assert by == {"application": 120.0, "database": 60.0, "infrastructure": 20.0}
    assert next(r for r in rows if r["group"] == "application")["mem_bytes_max"] == 1048576.0
    assert resources.missing_components("monolith", rows) == set()


def test_sampler_thread_starts_and_stops(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Регрессия (первый прогон на Timeweb): атрибут _stop перекрывал Thread._stop в join()."""
    from experiments.orchestrator import samplers

    monkeypatch.setattr(samplers.shutil, "which", lambda _: None)
    s = samplers.PodsSampler(tmp_path / "pods.csv", ["arch-bench"], step_s=0.1)
    s.start()
    s.stop()
    assert not s.is_alive()
    assert s.errors == ["kubectl не найден"]


def test_pg_sampler_survives_lost_connection(tmp_path: Path) -> None:
    """Регрессия (Timeweb, microservices): обрыв соединения убивал поток сэмплера молча."""
    import time

    from experiments.orchestrator.samplers import PgConnectionsSampler

    s = PgConnectionsSampler(tmp_path / "pg.csv", "postgresql://u:p@127.0.0.1:1/db", step_s=0.1)
    s.start()
    time.sleep(0.6)
    assert s.is_alive()  # не умер на первой ошибке
    s.stop()
    assert not s.is_alive()
    assert s.errors and all("Error" in e for e in s.errors)


def test_node_total_not_summed_across_sources() -> None:
    """Регрессия: id="/" есть и у cAdvisor, и у kubelet — сумма удваивала CPU узла."""
    q = resources.root_cpu_query(300)
    assert q.startswith("max(") and 'id="/"' in q


def test_blind_cadvisor_is_detected() -> None:
    """Docker 29 + containerd-snapshotter: cAdvisor видит только cgroup узла, не контейнеры."""
    rows = resources.rows_from_result("monolith", [], node_total=200.0)
    assert [r["group"] for r in rows] == ["infrastructure"]  # весь CPU ушёл бы в инфраструктуру
    assert resources.missing_components("monolith", rows) == {"app", "postgres"}
    assert resources.missing_components("three_tier", rows) == {"backend", "frontend", "postgres"}


T0 = 1_900_000_000.0


def _step(
    tmp: Path,
    cfg: BenchConfig,
    *,
    status4xx: int = 0,
    cpu: float = 0.3,
    gap: bool = False,
    dropped: int = 0,
) -> Path:
    d = tmp / "step"
    start = T0
    t_m = start + cfg.load.ramp_s + cfg.load.warmup_s
    pts = [start_point(start)]
    for k in range(1000):
        pts.append(point(t_m + k * 0.3, 10.0, 404 if k < status4xx else 200, "feed", "measure"))
    write_points(d / "points.json.gz", pts)
    (d / "k6_summary.json").write_text(
        json.dumps({"metrics": {"dropped_iterations": {"count": dropped}}}), encoding="utf-8"
    )
    with (d / "generator_cpu.csv").open("w", encoding="utf-8") as f:
        f.write("t,cpu_busy_share\n")
        for s in range(cfg.load.measure_s + 1):
            f.write(f"{t_m + s},{cpu}\n")
    with (d / "pg_connections.csv").open("w", encoding="utf-8") as f:
        f.write("t,state,n\n")
        for s in range(0, cfg.load.measure_s + 1, 5):
            if gap and 100 < s < 200:
                continue
            f.write(f"{t_m + s},active,10\n")
    return d


def test_valid_step_has_no_reasons(tmp_path: Path, cfg) -> None:  # type: ignore[no-untyped-def]
    assert invalid.evaluate_step(_step(tmp_path, cfg), cfg, 0, ["pg_connections.csv"]) == []


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"status4xx": 50}, "4xx"),
        ({"cpu": 0.9}, "CPU генератора"),
        ({"gap": True}, "пропуск мониторинга"),
        ({"dropped": 3}, "dropped_iterations"),
    ],
)
def test_invalid_criteria(tmp_path: Path, cfg, kw: dict, needle: str) -> None:  # type: ignore[no-untyped-def,type-arg]
    reasons = invalid.evaluate_step(_step(tmp_path, cfg, **kw), cfg, 0, ["pg_connections.csv"])
    assert any(needle in r for r in reasons), reasons


def test_k6_crash_is_invalid(tmp_path: Path, cfg) -> None:  # type: ignore[no-untyped-def]
    reasons = invalid.evaluate_step(_step(tmp_path, cfg), cfg, 107, [])
    assert any("кодом 107" in r for r in reasons)


def test_remote_k6_raises_fd_limit_before_start(cfg, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Без ulimit 5000 VU упираются в 1024 дескриптора генератора — ошибки генератора, не SUT."""
    from experiments.orchestrator.k6runner import K6_NOFILE, K6Runner

    class FakeShell:
        dry_run = False

        def __init__(self) -> None:
            self.cmds: list[list[str]] = []

        def run(self, cmd: list[str], **_: object) -> str:
            self.cmds.append(cmd)
            return "K6_EXIT=0"

    monkeypatch.setenv("GENERATOR_SSH", "u@gen")
    sh = FakeShell()
    res = K6Runner(cfg, sh).run_step(tmp_path, {"VUS": "5000"})  # type: ignore[arg-type]
    remote = sh.cmds[0][2]
    assert f"ulimit -n {K6_NOFILE} && " in remote
    assert remote.index("ulimit -n") < remote.index("k6 run")
    assert res.exit_code == 0
