"""Генерация всех артефактов развёртывания из experiments/configs/bench.yaml.

    python -m infrastructure.render            # записать generated/ и корневые compose
    python -m infrastructure.render --check    # код 1, если записанные файлы устарели

Сгенерированные файлы хранятся в git (чтобы `docker compose up -d` работал сразу после клона)
и не редактируются вручную; тест tests/unit/test_render.py проверяет отсутствие расхождений.
Секреты в файлы не попадают.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from experiments.config import REPO_ROOT, BenchConfig, load_bench_config
from infrastructure import compose, k8s, monitoring, nginx, openfaas, resources_doc
from infrastructure.common import NAMESPACE, check_process_rule, postgresql_conf

GENERATED = REPO_ROOT / "infrastructure" / "generated"


class _NoAliasDumper(yaml.SafeDumper):
    """Без якорей YAML: не все инструменты (faas-cli, kubectl) их одинаково понимают."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _yaml(doc: Any) -> str:
    return compose.header() + yaml.dump(
        doc, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False
    )


def _yaml_all(docs: list[Any]) -> str:
    return compose.header() + yaml.dump_all(
        docs, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False
    )


def resource_summary(cfg: BenchConfig) -> dict[str, Any]:
    out: dict[str, Any] = {"budget": cfg.budget.model_dump(), "architectures": {}}
    for name, arch in cfg.architectures.items():
        out["architectures"][name] = {
            "components": {
                c: {
                    **comp.model_dump(),
                    "cpu_request": comp.cpu,
                    "cpu_limit": comp.cpu,
                    "mem_request_mib": comp.mem_mib,
                    "mem_limit_mib": comp.mem_mib,
                }
                for c, comp in arch.components.items()
            },
            "max_request_path_cpu": arch.max_cpu(),
            "max_request_path_mem_mib": arch.max_mem_mib(),
            "max_db_connections": arch.max_db_connections(),
            "postgres": {"cpu": cfg.budget.postgres_vcpu, "mem_mib": cfg.budget.postgres_mem_mib},
            "hpa": arch.hpa.model_dump() if arch.hpa else None,
        }
    return out


def files(cfg: BenchConfig) -> dict[Path, str]:
    """Все генерируемые файлы: путь → содержимое."""
    check_process_rule(cfg)
    g = GENERATED
    out: dict[Path, str] = {
        g / "postgres" / "postgresql.conf": postgresql_conf(cfg),
        g / "compose" / "monolith.yml": _yaml(compose.monolith(cfg)),
        g / "compose" / "three-tier.yml": _yaml(compose.three_tier(cfg)),
        g / "compose" / "monitoring.yml": _yaml(compose.monitoring(cfg)),
        g / "nginx" / "three-tier.conf": nginx.three_tier(cfg),
        g / "nginx" / "ui-microservices.conf": nginx.ui_microservices(cfg),
        g / "nginx" / "ui-serverless.conf": nginx.ui_serverless(cfg),
        g
        / "kubernetes"
        / "postgres.yaml": _yaml_all([k8s.namespace(NAMESPACE), *k8s.postgres(cfg)]),
        g / "kubernetes" / "migrate-job.yaml": _yaml(k8s.migrate_job(cfg)),
        g / "kubernetes" / "microservices.yaml": _yaml_all(k8s.microservices(cfg)),
        g
        / "kubernetes"
        / "ui-microservices.yaml": _yaml_all(
            k8s.ui(cfg, "microservices", cfg.network.ui_microservices_node_port)
        ),
        g
        / "kubernetes"
        / "ui-serverless.yaml": _yaml_all(
            k8s.ui(cfg, "serverless", cfg.network.ui_serverless_node_port)
        ),
        g
        / "kubernetes"
        / "monitoring.yaml": _yaml_all([*k8s.kube_state_metrics(cfg), *k8s.prometheus_rbac()]),
        g / "openfaas" / "stack.yml": _yaml(openfaas.stack(cfg)),
        g / "openfaas" / "hpa.yaml": _yaml_all(openfaas.hpas(cfg)),
        g / "openfaas" / "values.yaml": _yaml(openfaas.helm_values(cfg)),
        g / "monitoring" / "prometheus.yml": _yaml(monitoring.prometheus_yml(cfg)),
        g
        / "monitoring"
        / "grafana"
        / "provisioning"
        / "datasources"
        / "prometheus.yml": _yaml(monitoring.datasource()),
        g
        / "monitoring"
        / "grafana"
        / "provisioning"
        / "dashboards"
        / "arch-bench.yml": _yaml(monitoring.dashboard_provider()),
        g / "resources.json": json.dumps(resource_summary(cfg), ensure_ascii=False, indent=2)
        + "\n",
        REPO_ROOT / "docs" / "generated" / "resource-tables.md": resources_doc.tables(cfg),
        REPO_ROOT / "docker-compose.yml": _yaml(compose.root_compose()),
        REPO_ROOT / "docker-compose.dev.yml": _yaml(compose.dev_override(cfg)),
        REPO_ROOT / "docker-compose.test.yml": _yaml(compose.test_compose(cfg)),
    }
    for name, dash in monitoring.dashboards().items():
        out[g / "monitoring" / "grafana" / "dashboards" / name] = monitoring.dump(dash)
    return out


def write(cfg: BenchConfig) -> list[Path]:
    written = []
    for path, content in files(cfg).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def stale(cfg: BenchConfig) -> list[Path]:
    return [
        p
        for p, content in files(cfg).items()
        if not p.is_file() or p.read_text(encoding="utf-8") != content
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="infrastructure.render")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_bench_config(args.config) if args.config else load_bench_config()
    if args.check:
        bad = stale(cfg)
        for p in bad:
            print(f"устарел: {p.relative_to(REPO_ROOT)}")
        return 1 if bad else 0
    for p in write(cfg):
        print(p.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
