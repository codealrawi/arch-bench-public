"""Общие определения генератора развёртываний."""

from __future__ import annotations

import math

from experiments.config import BenchConfig, ConfigError

NAMESPACE = "arch-bench"
FN_NAMESPACE = "openfaas-fn"
OF_NAMESPACE = "openfaas"
DB_NAME = "bench"
DB_USER = "bench"
SECRET_NAME = "arch-bench-secrets"
GENERATED_HEADER = "Сгенерировано infrastructure/render.py из experiments/configs/bench.yaml"

# (архитектура, компонент) → фабрика ASGI-приложения
FACTORIES = {
    ("monolith", "app"): "apps.monolith.app:create_app",
    ("three_tier", "backend"): "apps.monolith.app:create_app",
    ("microservices", "gateway"): "apps.gateway.app:create_app",
    ("microservices", "auth"): "apps.services.app:create_auth",
    ("microservices", "content"): "apps.services.app:create_content",
    ("microservices", "moderation"): "apps.services.app:create_moderation",
    ("microservices", "analytics"): "apps.services.app:create_analytics",
    ("serverless", "fn_auth"): "serverless.functions.handlers:create_auth",
    ("serverless", "fn_content"): "serverless.functions.handlers:create_content",
    ("serverless", "fn_stats"): "serverless.functions.handlers:create_stats",
}
PROXIES = {("three_tier", "frontend"), ("serverless", "of_gateway")}
NO_DB = {("microservices", "gateway"), ("microservices", "moderation")}


def processes_rule(cpu: float) -> int:
    """Правило параллелизма (resource-allocation.md §1.6): 1 процесс на 1 vCPU, минимум 1."""
    return max(1, math.floor(cpu + 1e-9))


def check_process_rule(cfg: BenchConfig) -> None:
    for arch_name, arch in cfg.architectures.items():
        for comp_name, comp in arch.components.items():
            if (arch_name, comp_name) in PROXIES:
                continue
            expected = processes_rule(comp.cpu)
            if comp.workers != expected:
                raise ConfigError(
                    f"{arch_name}.{comp_name}: workers={comp.workers}, по правилу "
                    f"1/vCPU должно быть {expected}"
                )
        for key in arch.components:
            if (arch_name, key) not in FACTORIES and (arch_name, key) not in PROXIES:
                raise ConfigError(f"{arch_name}.{key}: нет фабрики приложения")


def millicpu(cpu: float) -> str:
    return f"{int(round(cpu * 1000))}m"


def postgresql_conf(cfg: BenchConfig) -> str:
    lines = [
        f"# {GENERATED_HEADER} — не редактировать.",
        f"max_connections = {cfg.postgres.max_connections}",
    ]
    for key, value in sorted(cfg.postgres.settings.items()):
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"
