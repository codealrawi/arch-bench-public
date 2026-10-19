"""CPU и память по контейнерам за окно измерения из Prometheus (cAdvisor) → resources.csv.

Группы (требование автора: раздельный учёт, resource-allocation.md §7):
  application    — компоненты приложения на пути запроса (app, backend, auth, content,
                   moderation, analytics, функции);
  gateway        — маршрутизаторы на пути запроса (nginx 3-tier, API-gateway, шлюз OpenFaaS);
  database       — PostgreSQL;
  infrastructure — всё остальное: k3s, OpenFaaS core, мониторинг, ОС (= весь узел − остальное).
Путь запроса (бюджет 4 vCPU) = application + gateway.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

APPLICATION = {
    "monolith": {"app"},
    "three_tier": {"backend"},
    "microservices": {"auth", "content", "moderation", "analytics"},
    "serverless": {"auth", "content", "stats"},
}
GATEWAY = {"three_tier": {"frontend"}, "microservices": {"gateway"}, "serverless": {"gateway"}}
APP_NAMESPACES = {
    "monolith": {""},
    "three_tier": {""},
    "microservices": {"arch-bench"},
    "serverless": {"openfaas", "openfaas-fn"},  # шлюз OpenFaaS — в openfaas, функции — в -fn
}
GROUPS = ("application", "gateway", "database", "infrastructure")
FIELDS = ["group", "component", "namespace", "cpu_seconds", "mem_bytes_max"]

# docker compose: <project>-<service>-<n>; k8s: pod <deployment>-<hash>-<hash>
_COMPOSE_RE = re.compile(r"^arch-bench-[a-z-]+?-(?P<svc>[a-z_]+)-\d+$")
_POD_RE = re.compile(r"^(?P<dep>[a-z0-9-]+?)-[a-z0-9]{6,10}-[a-z0-9]{5}$")


def component_of(labels: dict[str, str]) -> str | None:
    if labels.get("pod"):
        m = _POD_RE.match(labels["pod"])
        return m.group("dep") if m else labels["pod"]
    name = labels.get("name", "")
    m = _COMPOSE_RE.match(name)
    return m.group("svc") if m else (name or None)


def group_of(architecture: str, labels: dict[str, str]) -> str:
    comp = component_of(labels) or ""
    ns = labels.get("namespace", "")
    if comp == "postgres":
        return "database"
    if ns in APP_NAMESPACES[architecture]:
        if comp in APPLICATION[architecture]:
            return "application"
        if comp in GATEWAY.get(architecture, set()):
            return "gateway"
    return "infrastructure"


def cpu_query(window_s: int) -> str:
    # increase() по счётчику CPU-секунд; container!="POD" отсекает pause-контейнеры
    return (
        f"sum by (name, namespace, pod) (increase(container_cpu_usage_seconds_total"
        f'{{container!="POD", image!=""}}[{window_s}s]))'
    )


def mem_query(window_s: int) -> str:
    return (
        f"sum by (name, namespace, pod) (max_over_time(container_memory_working_set_bytes"
        f'{{container!="POD", image!=""}}[{window_s}s]))'
    )


def root_cpu_query(window_s: int) -> str:
    # max, а не sum: корневой cgroup узла (id="/") отдают ОБА источника — cAdvisor (docker) и
    # kubelet; sum удваивал CPU узла и завышал infrastructure (пробный прогон Timeweb, 2026-10-05)
    return f'max(increase(container_cpu_usage_seconds_total{{id="/"}}[{window_s}s]))'


def _query(prom_url: str, q: str, t: float) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{prom_url.rstrip('/')}/api/v1/query", params={"query": q, "time": f"{t:.3f}"}, timeout=30
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus: {body}")
    return list(body["data"]["result"])


def _key(labels: dict[str, str]) -> tuple[str, str, str]:
    return labels.get("name", ""), labels.get("namespace", ""), labels.get("pod", "")


def rows_from_result(
    architecture: str,
    cpu: Iterable[dict[str, Any]],
    node_total: float | None,
    mem: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    mem_by = {
        _key({k: str(v) for k, v in m.get("metric", {}).items()}): float(m["value"][1]) for m in mem
    }
    rows: list[dict[str, Any]] = []
    for item in cpu:
        labels = {k: str(v) for k, v in item.get("metric", {}).items()}
        rows.append(
            {
                "group": group_of(architecture, labels),
                "component": component_of(labels) or "unknown",
                "namespace": labels.get("namespace", ""),
                "cpu_seconds": float(item["value"][1]),
                "mem_bytes_max": mem_by.get(_key(labels), 0.0),
            }
        )
    if node_total is not None:
        workload = sum(float(r["cpu_seconds"]) for r in rows if r["group"] != "infrastructure")
        infra_mem = sum(float(r["mem_bytes_max"]) for r in rows if r["group"] == "infrastructure")
        rows = [r for r in rows if r["group"] != "infrastructure"]
        rows.append(
            {
                "group": "infrastructure",
                "component": "node_minus_workload",
                "namespace": "",
                "cpu_seconds": max(0.0, node_total - workload),
                "mem_bytes_max": infra_mem,
            }
        )
    return rows


def missing_components(architecture: str, rows: Iterable[dict[str, Any]]) -> set[str]:
    """Компоненты пути запроса и БД, по которым cAdvisor не дал ни одной серии.

    Без этой проверки «слепой» cAdvisor (например, Docker с containerd-snapshotter) молча
    переносит CPU приложения в infrastructure = узел − остальное.
    """
    expected = APPLICATION[architecture] | GATEWAY.get(architecture, set()) | {"postgres"}
    seen = {str(r["component"]) for r in rows if r["group"] != "infrastructure"}
    return expected - seen


def export(prom_url: str, architecture: str, t_end: float, window_s: int, out: Path) -> Path:
    cpu = _query(prom_url, cpu_query(window_s), t_end)
    mem = _query(prom_url, mem_query(window_s), t_end)
    root = _query(prom_url, root_cpu_query(window_s), t_end)
    node_total = float(root[0]["value"][1]) if root else None
    rows = rows_from_result(architecture, cpu, node_total, mem)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    missing = missing_components(architecture, rows)
    if missing:
        raise RuntimeError(
            f"cAdvisor/Prometheus: нет серий CPU для {sorted(missing)} ({architecture}); "
            f"учёт ресурсов неполон — см. {out}"
        )
    return out
