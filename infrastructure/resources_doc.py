"""Таблицы распределения ресурсов для docs/resource-allocation.md.

Строятся из bench.yaml, без ручных чисел.
"""

from __future__ import annotations

from experiments.config import BenchConfig

ROLE = {
    ("three_tier", "frontend"): "шлюз (nginx)",
    ("microservices", "gateway"): "шлюз",
    ("serverless", "of_gateway"): "шлюз OpenFaaS",
}


def tables(cfg: BenchConfig) -> str:
    b = cfg.budget
    lines = [
        "<!-- Сгенерировано infrastructure/render.py из bench.yaml — не редактировать. -->",
        f"Бюджет узла: {b.total_vcpu:g} vCPU / {b.total_mem_mib} MiB = путь запроса "
        f"{b.request_path_vcpu:g} vCPU / {b.request_path_mem_mib} MiB + PostgreSQL "
        f"{b.postgres_vcpu:g} vCPU / {b.postgres_mem_mib} MiB + инфраструктура "
        f"{b.infra_reserve_vcpu:g} vCPU / {b.infra_reserve_mem_mib} MiB.",
        "",
    ]
    for name, arch in cfg.architectures.items():
        lines += [
            f"### {name}",
            "",
            "| Компонент | Роль | CPU request = limit | Memory request = limit | min | max | "
            "процессов | пул БД на процесс | max CPU |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c, comp in arch.components.items():
            lines.append(
                f"| {c} | {ROLE.get((name, c), 'приложение')} | {comp.cpu:g} | {comp.mem_mib} MiB"
                f" | {comp.min} | {comp.max} | {comp.workers or 'nginx/шлюз'} | {comp.db_pool} | "
                f"{comp.cpu * comp.max:g} |"
            )
        hpa = (
            f"HPA: target CPU {arch.hpa.target_cpu} %, scaleUp stabilization "
            f"{arch.hpa.scale_up_stabilization_s} с, scaleDown "
            f"{arch.hpa.scale_down_stabilization_s} с"
            if arch.hpa
            else "HPA: нет"
        )
        lines += [
            "",
            f"Σ max CPU пути запроса = **{arch.max_cpu():g} vCPU**; Σ соединений БД ≤ "
            f"**{arch.max_db_connections()}** (из {cfg.postgres.total_app_pool}). {hpa}. "
            f"PostgreSQL: {b.postgres_vcpu:g} vCPU / {b.postgres_mem_mib} MiB.",
            "",
        ]
    return "\n".join(lines) + "\n"
