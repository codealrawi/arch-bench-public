"""OpenFaaS CE (Configuration D): stack.yml, HPA функций, helm values шлюза.

- функции — тот же образ приложения (arch-bench-app), что у остальных архитектур; шаблон
  serverless/template/arch-bench-asgi описывает тот же образ для `faas-cli build`;
- масштабирование — Kubernetes HPA (протокол P-33): встроенный автоскейлер OpenFaaS CE
  (alertmanager → шлюз) не создаётся (alertmanager.create=false). Метку
  com.openfaas.scale.factor=0 шлюз CE 15.0.13 отвергает («bad scaling factor: 0», функция
  не находится — проверено на Timeweb 2026-10-05); scale-to-zero выключен (P-34), min ≥ 1;
- пробы: faas-netes проверяет /_/health и /_/ready на порту 8080 (handlers._probes).
Секреты (JWT_SECRET, DATABASE_URL) faas-cli подставляет из окружения при развёртывании.
"""

from __future__ import annotations

from typing import Any

from experiments.config import BenchConfig
from infrastructure.common import (
    DB_NAME,
    DB_USER,
    FACTORIES,
    FN_NAMESPACE,
    NAMESPACE,
    PROXIES,
    millicpu,
)
from infrastructure.envs import component_env
from infrastructure.k8s import hpa, resources


def function_names(cfg: BenchConfig) -> list[str]:
    return [
        c.removeprefix("fn_")
        for c in cfg.architectures["serverless"].components
        if ("serverless", c) not in PROXIES
    ]


def stack(cfg: BenchConfig) -> dict[str, Any]:
    arch = cfg.architectures["serverless"]
    port = cfg.network.function_port
    functions: dict[str, Any] = {}
    for comp_name, comp in arch.components.items():
        if ("serverless", comp_name) in PROXIES:
            continue
        fn = comp_name.removeprefix("fn_")
        r = {"cpu": millicpu(comp.cpu), "memory": f"{comp.mem_mib}Mi"}
        env = component_env(
            cfg,
            comp,
            service=f"serverless-{fn}",
            factory=FACTORIES[("serverless", comp_name)],
            port=port,
            uses_db=True,
            extra={"HEALTH_PATH": "/_/health"},
        )
        env |= {
            "JWT_SECRET": "${JWT_SECRET}",
            "DATABASE_URL": f"postgresql://{DB_USER}:${{POSTGRES_PASSWORD}}@postgres."
            f"{NAMESPACE}.svc.cluster.local:5432/{DB_NAME}",
        }
        functions[fn] = {
            "lang": "arch-bench-asgi",
            "handler": "./serverless/functions",
            "image": cfg.images.app,
            "skip_build": True,
            "environment": env,
            "requests": dict(r),
            "limits": dict(r),
            "labels": {
                "com.openfaas.scale.min": str(comp.min),
                "com.openfaas.scale.max": str(comp.max),
                "com.openfaas.scale.zero": "false",
                "arch-bench/role": "request-path",
                "arch-bench/arch": "serverless",
            },
            "annotations": {
                "com.openfaas.health.http.path": "/_/health",
                "com.openfaas.ready.http.path": "/_/ready",
                "com.openfaas.health.http.initialDelay": "10s",
                "prometheus.io/scrape": "true",
                "prometheus.io/port": str(port),
                "prometheus.io/path": "/metrics",
            },
        }
    return {
        "version": 1.0,
        "provider": {"name": "openfaas", "gateway": "${OPENFAAS_URL}"},
        "functions": functions,
    }


def hpas(cfg: BenchConfig) -> list[dict[str, Any]]:
    arch = cfg.architectures["serverless"]
    assert arch.hpa is not None
    return [
        hpa(arch.hpa, c.removeprefix("fn_"), FN_NAMESPACE, comp)
        for c, comp in arch.components.items()
        if ("serverless", c) not in PROXIES and comp.max > comp.min
    ]


def helm_values(cfg: BenchConfig) -> dict[str, Any]:
    gw = cfg.architectures["serverless"].components["of_gateway"]
    t = int(cfg.network.upstream_timeout_s)
    return {
        "functionNamespace": FN_NAMESPACE,
        "generateBasicAuth": True,
        "openfaasPro": False,
        "exposeServices": True,
        "serviceType": "NodePort",
        "httpProbe": True,
        "gateway": {
            "replicas": gw.max,
            "nodePort": cfg.network.openfaas_node_port,
            "upstreamTimeout": f"{t}s",
            "readTimeout": f"{t + 5}s",
            "writeTimeout": f"{t + 5}s",
            "resources": resources(gw.cpu, gw.mem_mib),
        },
        "queueWorker": {"replicas": 1},
        # без alertmanager нет встроенного автомасштабирования CE: реплики меняет только HPA
        "alertmanager": {"create": False},
    }


def template_yml() -> str:
    return (
        "language: arch-bench-asgi\n"
        "# Тот же образ, что у всех архитектур: функция = ASGI-приложение\n"
        "# serverless.functions.handlers:create_<name> (APP_FACTORY в stack.yml).\n"
        "fprocess: /usr/local/bin/entrypoint.sh\n"
    )
