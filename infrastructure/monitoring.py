"""Prometheus + Grafana: конфигурация сбора и четыре дашборда (Application, Infrastructure,
PostgreSQL, Kubernetes). Prometheus работает на узле (network_mode: host) и видит:
- cAdvisor (контейнеры Docker: A, B), node_exporter (узел), postgres_exporter (БД);
- приложения A/B: /metrics на опубликованных портах;
- k3s: поды по аннотациям prometheus.io/* (kubernetes_sd через API-сервер), cAdvisor kubelet
  (CPU/RAM подов), kube-state-metrics (реплики, HPA).
"""

from __future__ import annotations

import json
from typing import Any

from experiments.config import BenchConfig
from infrastructure.compose import BACKEND_METRICS_PORT
from infrastructure.k8s import KSM_NODE_PORT

TOKEN = "/etc/prometheus/k3s-token"
K8S_API = "https://127.0.0.1:6443"


def prometheus_yml(cfg: BenchConfig) -> dict[str, Any]:
    step = f"{cfg.metrics.monitoring_step_s}s"
    k8s_auth = {
        "authorization": {"credentials_file": TOKEN},
        "tls_config": {"insecure_skip_verify": True},
    }
    return {
        "global": {"scrape_interval": step, "evaluation_interval": step},
        "scrape_configs": [
            {"job_name": "cadvisor", "static_configs": [{"targets": ["127.0.0.1:8081"]}]},
            {"job_name": "node", "static_configs": [{"targets": ["127.0.0.1:9100"]}]},
            {"job_name": "postgres", "static_configs": [{"targets": ["127.0.0.1:9187"]}]},
            {
                "job_name": "app-compose",
                "static_configs": [
                    {
                        "targets": [f"127.0.0.1:{cfg.network.monolith_port}"],
                        "labels": {"arch": "monolith"},
                    },
                    {
                        "targets": [f"127.0.0.1:{BACKEND_METRICS_PORT}"],
                        "labels": {"arch": "three_tier"},
                    },
                ],
            },
            {
                "job_name": "kubelet-cadvisor",
                "scheme": "https",
                "metrics_path": "/metrics/cadvisor",
                "static_configs": [{"targets": ["127.0.0.1:10250"]}],
                **k8s_auth,
            },
            {
                "job_name": "kube-state-metrics",
                "static_configs": [{"targets": [f"127.0.0.1:{KSM_NODE_PORT}"]}],
            },
            {
                "job_name": "k8s-pods",
                "kubernetes_sd_configs": [{"role": "pod", "api_server": K8S_API, **k8s_auth}],
                "relabel_configs": [
                    {
                        "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                        "action": "keep",
                        "regex": "true",
                    },
                    {
                        "source_labels": [
                            "__address__",
                            "__meta_kubernetes_pod_annotation_prometheus_io_port",
                        ],
                        "action": "replace",
                        "regex": r"([^:]+)(?::\d+)?;(\d+)",
                        "replacement": "$1:$2",
                        "target_label": "__address__",
                    },
                    {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                    {"source_labels": ["__meta_kubernetes_pod_name"], "target_label": "pod"},
                ],
            },
        ],
    }


# ── Grafana ─────────────────────────────────────────────────────────────────────────────
def datasource() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Prometheus",
                "uid": "prometheus",
                "type": "prometheus",
                "access": "proxy",
                "url": "http://127.0.0.1:9090",
                "isDefault": True,
            }
        ],
    }


def dashboard_provider() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "providers": [
            {
                "name": "arch-bench",
                "folder": "arch-bench",
                "type": "file",
                "options": {"path": "/var/lib/grafana/dashboards"},
            }
        ],
    }


def _panel(
    pid: int, title: str, exprs: list[tuple[str, str]], unit: str, x: int, y: int
) -> dict[str, Any]:
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"refId": chr(65 + i), "expr": e, "legendFormat": legend}
            for i, (e, legend) in enumerate(exprs)
        ],
    }


def _dashboard(
    uid: str, title: str, panels: list[tuple[str, list[tuple[str, str]], str]]
) -> dict[str, Any]:
    out = []
    for i, (t, exprs, unit) in enumerate(panels):
        out.append(_panel(i + 1, t, exprs, unit, (i % 2) * 12, (i // 2) * 8))
    return {
        "uid": uid,
        "title": title,
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-30m", "to": "now"},
        "refresh": "10s",
        "panels": out,
        "tags": ["arch-bench"],
    }


def dashboards() -> dict[str, dict[str, Any]]:
    w = "[1m]"
    app = _dashboard(
        "arch-bench-app",
        "Application",
        [
            (
                "RPS по сервисам",
                [(f"sum by (service) (rate(app_http_requests_total{w}))", "{{service}}")],
                "reqps",
            ),
            (
                "Латентность p50 / p95 / p99",
                [
                    (
                        f"histogram_quantile({q}, sum by (le) (rate("
                        "app_http_request_duration_seconds_bucket"
                        f"{w})))",
                        f"p{int(q * 100)}",
                    )
                    for q in (0.5, 0.95, 0.99)
                ],
                "s",
            ),
            (
                "Ошибки 5xx",
                [
                    (
                        f'sum by (service) (rate(app_http_requests_total{{status=~"5.."}}{w}))',
                        "{{service}}",
                    )
                ],
                "reqps",
            ),
            (
                "Ответы 4xx",
                [
                    (
                        f'sum by (service) (rate(app_http_requests_total{{status=~"4.."}}{w}))',
                        "{{service}}",
                    )
                ],
                "reqps",
            ),
            (
                "p95 по маршрутам",
                [
                    (
                        f"histogram_quantile(0.95, sum by (le, route) (rate("
                        f"app_http_request_duration_seconds_bucket{w})))",
                        "{{route}}",
                    )
                ],
                "s",
            ),
        ],
    )
    infra = _dashboard(
        "arch-bench-infra",
        "Infrastructure",
        [
            (
                "CPU узла",
                [(f'1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}{w}))', "busy")],
                "percentunit",
            ),
            (
                "RAM узла",
                [("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used")],
                "bytes",
            ),
            (
                "Сеть",
                [
                    (f'sum(rate(node_network_receive_bytes_total{{device!="lo"}}{w}))', "rx"),
                    (f'sum(rate(node_network_transmit_bytes_total{{device!="lo"}}{w}))', "tx"),
                ],
                "Bps",
            ),
            (
                "CPU контейнеров Docker",
                [
                    (
                        f"sum by (name) (rate(container_cpu_usage_seconds_total"
                        f'{{job="cadvisor",name!=""}}{w}))',
                        "{{name}}",
                    )
                ],
                "none",
            ),
            (
                "Память контейнеров Docker",
                [
                    (
                        "sum by (name) (container_memory_working_set_bytes"
                        '{job="cadvisor",name!=""})',
                        "{{name}}",
                    )
                ],
                "bytes",
            ),
        ],
    )
    pg = _dashboard(
        "arch-bench-postgres",
        "PostgreSQL",
        [
            (
                "Соединения по состояниям",
                [("sum by (state) (pg_stat_activity_count)", "{{state}}")],
                "none",
            ),
            (
                "Средняя длительность запроса (pg_stat_statements)",
                [
                    (
                        f"sum(rate(pg_stat_statements_seconds_total{w})) / "
                        f"sum(rate(pg_stat_statements_calls_total{w}))",
                        "mean",
                    )
                ],
                "s",
            ),
            (
                "Запросов в секунду",
                [(f"sum(rate(pg_stat_statements_calls_total{w}))", "calls")],
                "ops",
            ),
            (
                "CPU PostgreSQL",
                [
                    (
                        f'sum(rate(container_cpu_usage_seconds_total{{name=~".*postgres.*"}}'
                        f"{w})) or sum(rate(container_cpu_usage_seconds_total"
                        f'{{pod=~"postgres-.*",container="postgres"}}{w}))',
                        "cpu",
                    )
                ],
                "none",
            ),
            (
                "Память PostgreSQL",
                [
                    (
                        'sum(container_memory_working_set_bytes{name=~".*postgres.*"}) '
                        'or sum(container_memory_working_set_bytes{pod=~"postgres-.*",'
                        'container="postgres"})',
                        "mem",
                    )
                ],
                "bytes",
            ),
            (
                "Транзакции",
                [
                    (f"sum(rate(pg_stat_database_xact_commit{w}))", "commit"),
                    (f"sum(rate(pg_stat_database_xact_rollback{w}))", "rollback"),
                ],
                "ops",
            ),
        ],
    )
    k8s = _dashboard(
        "arch-bench-k8s",
        "Kubernetes",
        [
            (
                "Поды (готовые реплики)",
                [
                    (
                        "sum by (deployment) (kube_deployment_status_replicas_ready"
                        '{namespace=~"arch-bench|openfaas-fn"})',
                        "{{deployment}}",
                    )
                ],
                "none",
            ),
            (
                "HPA: текущие / желаемые реплики",
                [
                    (
                        "kube_horizontalpodautoscaler_status_current_replicas",
                        "{{horizontalpodautoscaler}}" " current",
                    ),
                    (
                        "kube_horizontalpodautoscaler_status_desired_replicas",
                        "{{horizontalpodautoscaler}}" " desired",
                    ),
                ],
                "none",
            ),
            (
                "CPU подов",
                [
                    (
                        f"sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace=~"
                        f'"arch-bench|openfaas-fn|openfaas",container!=""}}{w}))',
                        "{{pod}}",
                    )
                ],
                "none",
            ),
            (
                "Память подов",
                [
                    (
                        "sum by (pod) (container_memory_working_set_bytes{namespace=~"
                        '"arch-bench|openfaas-fn|openfaas",container!=""})',
                        "{{pod}}",
                    )
                ],
                "bytes",
            ),
        ],
    )
    return {
        "application.json": app,
        "infrastructure.json": infra,
        "postgresql.json": pg,
        "kubernetes.json": k8s,
    }


def dump(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
