"""Сгенерированные артефакты развёртывания соответствуют bench.yaml и протоколу."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from experiments.config import BenchConfig, ConfigError, load_bench_config
from infrastructure import compose, k8s, nginx, openfaas, render
from infrastructure.common import check_process_rule, postgresql_conf


@pytest.fixture(scope="module")
def cfg() -> BenchConfig:
    return load_bench_config()


def _cpu(v: str) -> float:
    return int(v.removesuffix("m")) / 1000


def _by(docs: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_committed_files_are_up_to_date(cfg: BenchConfig) -> None:
    stale = render.stale(cfg)
    assert not stale, f"выполните python -m infrastructure.render: {stale}"


def test_process_rule(cfg: BenchConfig) -> None:
    check_process_rule(cfg)
    data = cfg.model_dump()
    data["architectures"]["monolith"]["components"]["app"]["workers"] = 3
    with pytest.raises(ConfigError, match="1/vCPU"):
        check_process_rule(BenchConfig.model_validate(data))


def test_equal_request_path_budget(cfg: BenchConfig) -> None:
    for name, arch in cfg.architectures.items():
        assert arch.max_cpu() == pytest.approx(cfg.budget.request_path_vcpu), name
        assert arch.max_db_connections() <= cfg.postgres.total_app_pool, name


def test_compose_monolith_and_three_tier(cfg: BenchConfig) -> None:
    m = compose.monolith(cfg)["services"]
    app = cfg.architectures["monolith"].components["app"]
    assert float(m["app"]["cpus"]) == app.cpu
    assert m["app"]["mem_limit"] == m["app"]["mem_reservation"] == f"{app.mem_mib}m"
    assert m["app"]["environment"]["APP_WORKERS"] == str(app.workers)
    assert m["app"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "frontend" not in m  # A — без nginx
    t = compose.three_tier(cfg)["services"]
    assert float(t["backend"]["cpus"]) + float(t["frontend"]["cpus"]) == pytest.approx(4.0)
    assert t["backend"]["ports"] == ["127.0.0.1:8001:8000"]  # наружу — только через nginx
    assert m["postgres"] == t["postgres"]  # одинаковая БД
    assert m["postgres"]["ports"] == ["127.0.0.1:5432:5432"]


def test_no_secrets_in_generated_files(cfg: BenchConfig) -> None:
    for path, content in render.files(cfg).items():
        for line in content.splitlines():
            if any(k in line for k in ("JWT_SECRET", "POSTGRES_PASSWORD", "ADMIN_PASSWORD")):
                ok = (
                    "${" in line
                    or "secretKeyRef" in line
                    or "key:" in line
                    or "name:" in line
                    or line.strip().startswith("#")
                )
                assert ok, f"{path}: {line}"


def test_nginx_three_tier(cfg: BenchConfig) -> None:
    conf = nginx.three_tier(cfg)
    assert "worker_processes 1;" in conf
    assert "location = /metrics { return 404; }" in conf
    assert "proxy_pass http://backend" in conf


def test_ui_serverless_rewrites_domains(cfg: BenchConfig) -> None:
    conf = nginx.ui_serverless(cfg)
    for fn in ("auth", "content", "stats"):
        assert f"/function/{fn}$1" in conf


def test_k8s_microservices(cfg: BenchConfig) -> None:
    docs = k8s.microservices(cfg)
    deps, hpas = _by(docs, "Deployment"), _by(docs, "HorizontalPodAutoscaler")
    arch = cfg.architectures["microservices"]
    for name, comp in arch.components.items():
        c = deps[name]["spec"]["template"]["spec"]["containers"][0]
        assert c["resources"]["requests"] == c["resources"]["limits"]
        assert _cpu(c["resources"]["limits"]["cpu"]) == comp.cpu
        assert c["readinessProbe"]["httpGet"]["path"] == "/readiness"
        assert c["livenessProbe"]["httpGet"]["path"] == "/health"
        assert "preStop" in c["lifecycle"]
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["AUTH_MODE"] == ("jwt" if name == "gateway" else "trusted-headers")
        if comp.max > comp.min:
            h = hpas[name]["spec"]
            assert (h["minReplicas"], h["maxReplicas"]) == (comp.min, comp.max)
            assert h["metrics"][0]["resource"]["target"]["averageUtilization"] == 70
    exposed = [n for n, s in _by(docs, "Service").items() if s["spec"].get("type") == "NodePort"]
    assert exposed == ["gateway"]


def test_least_privilege_secrets(cfg: BenchConfig) -> None:
    deps = _by(k8s.microservices(cfg), "Deployment")

    def secrets(n: str) -> set[str]:
        c = deps[n]["spec"]["template"]["spec"]["containers"][0]
        return {e["name"] for e in c["env"] if "valueFrom" in e}

    assert secrets("moderation") == set()
    assert secrets("gateway") == {"JWT_SECRET"}
    assert secrets("content") == {"JWT_SECRET", "DATABASE_URL"}


def test_compose_request_path_has_fd_headroom(cfg: BenchConfig) -> None:
    """Регрессия (серия 1, 2026-10-06): soft nofile 1024 в Docker — EOF/reset при 5000 VU у A/B."""
    from infrastructure import compose

    need = max(cfg.load.steps_server) * 4  # с запасом: соединения клиентов + к БД + служебные
    for spec in (compose.monolith(cfg), compose.three_tier(cfg)):
        for name in ("postgres", "app", "backend", "frontend"):
            svc = spec["services"].get(name)
            if svc is None:
                continue
            lim = svc["ulimits"]["nofile"]
            assert lim["soft"] == lim["hard"] >= need, (spec["name"], name)


def test_openfaas_stack_and_hpa(cfg: BenchConfig) -> None:
    st = openfaas.stack(cfg)["functions"]
    assert set(st) == {"auth", "content", "stats"}
    gw = cfg.architectures["serverless"].components["of_gateway"]
    total = gw.cpu * gw.max
    for fn, spec in st.items():
        comp = cfg.architectures["serverless"].components[f"fn_{fn}"]
        assert spec["requests"] == spec["limits"]
        assert spec["labels"]["com.openfaas.scale.zero"] == "false"
        # scale.factor=0 шлюз CE отвергает (Timeweb, 2026-10-05) — метки быть не должно
        assert "com.openfaas.scale.factor" not in spec["labels"]
        assert int(spec["labels"]["com.openfaas.scale.min"]) >= 1
        assert spec["image"] == cfg.images.app  # тот же образ
        assert spec["environment"]["AUTH_MODE"] == "jwt"
        total += _cpu(spec["limits"]["cpu"]) * comp.max
    assert total == pytest.approx(cfg.budget.request_path_vcpu)
    hp = {h["metadata"]["name"]: h for h in openfaas.hpas(cfg)}
    assert set(hp) == {"content"} and hp["content"]["metadata"]["namespace"] == "openfaas-fn"
    # масштабирует только HPA: встроенный автоскейлер CE (alertmanager) не создаётся
    assert openfaas.helm_values(cfg)["alertmanager"] == {"create": False}


def test_postgres_identical_everywhere(cfg: BenchConfig) -> None:
    cm = _by(k8s.postgres(cfg), "ConfigMap")["postgres-conf"]
    assert cm["data"]["postgresql.conf"] == postgresql_conf(cfg)
    assert "pg_stat_statements" in postgresql_conf(cfg)


def test_all_generated_yaml_valid(cfg: BenchConfig) -> None:
    for path, content in render.files(cfg).items():
        if path.suffix in (".yml", ".yaml"):
            list(yaml.safe_load_all(content))
