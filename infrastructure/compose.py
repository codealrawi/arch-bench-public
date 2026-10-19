"""Docker Compose: Configuration A (monolith), B (three-tier), мониторинг, dev/test.

Секреты — из .env (``${VAR:?...}``), в файлы не записываются. PostgreSQL публикуется только
на 127.0.0.1 (сброс БД оркестратором и экспортёр метрик), API — на всех интерфейсах.
"""

from __future__ import annotations

from typing import Any

from experiments.config import BenchConfig
from infrastructure.common import DB_NAME, DB_USER, FACTORIES, GENERATED_HEADER
from infrastructure.envs import component_env

BACKEND_METRICS_PORT = 8001
PG_PASSWORD = "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required (.env)}"
JWT = "${JWT_SECRET:?JWT_SECRET is required (.env)}"
DSN = f"postgresql://{DB_USER}:${{POSTGRES_PASSWORD}}@postgres:5432/{DB_NAME}"


def _mem(mib: int) -> str:
    return f"{mib}m"


# Docker 29 даёт контейнеру soft nofile = 1024 (поды k3s — 1048576). При 5000 VU процесс
# приложения упирался в 1024 сокета и закрывал соединения (EOF / reset у k6) — только у A/B.
# Значение = hard-лимит dockerd (LimitNOFILE=524288), заведомо больше числа соединений.
NOFILE = {"nofile": {"soft": 524288, "hard": 524288}}


def postgres_service(cfg: BenchConfig, conf_rel: str) -> dict[str, Any]:
    mem = _mem(cfg.budget.postgres_mem_mib)
    return {
        "image": cfg.postgres.image,
        "command": ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"],
        "environment": {
            "POSTGRES_DB": DB_NAME,
            "POSTGRES_USER": DB_USER,
            "POSTGRES_PASSWORD": PG_PASSWORD,
        },
        "volumes": [
            f"{conf_rel}:/etc/postgresql/postgresql.conf:ro",
            "pgdata:/var/lib/postgresql/data",
        ],
        "cpus": str(cfg.budget.postgres_vcpu),
        "mem_limit": mem,
        "mem_reservation": mem,
        "shm_size": "1g",
        "ulimits": NOFILE,
        "ports": ["127.0.0.1:5432:5432"],
        "healthcheck": {
            "test": ["CMD-SHELL", f"pg_isready -U {DB_USER} -d {DB_NAME}"],
            "interval": "2s",
            "timeout": "2s",
            "retries": 60,
        },
        "restart": "unless-stopped",
    }


def migrate_service(cfg: BenchConfig) -> dict[str, Any]:
    return {
        "image": cfg.images.app,
        "command": ["python", "-m", "database.manage", "migrate", "up"],
        "environment": {"DATABASE_URL": DSN},
        "depends_on": {"postgres": {"condition": "service_healthy"}},
        "restart": "no",
    }


def app_service(
    cfg: BenchConfig, arch: str, comp_name: str, *, serve_frontend: bool, publish: bool
) -> dict[str, Any]:
    comp = cfg.architectures[arch].components[comp_name]
    port = cfg.network.app_port
    extra = {"SERVE_FRONTEND": "true" if serve_frontend else "false", "HEALTH_PATH": "/health"}
    if arch == "three_tier":  # за nginx: адрес клиента — из X-Forwarded-For (rate limit)
        extra["TRUST_PROXY"] = "true"
    env = component_env(
        cfg,
        comp,
        service=f"{arch}-{comp_name}",
        factory=FACTORIES[(arch, comp_name)],
        port=port,
        uses_db=True,
        extra=extra,
    )
    mem = _mem(comp.mem_mib)
    svc: dict[str, Any] = {
        "image": cfg.images.app,
        "environment": env | {"JWT_SECRET": JWT, "DATABASE_URL": DSN},
        "cpus": str(comp.cpu),
        "mem_limit": mem,
        "mem_reservation": mem,
        "depends_on": {"migrate": {"condition": "service_completed_successfully"}},
        "stop_grace_period": f"{cfg.app.graceful_shutdown_s + 5}s",
        "ulimits": NOFILE,
        "restart": "unless-stopped",
        "labels": {"arch-bench.role": "request-path"},
    }
    if publish:
        svc["ports"] = [f"{cfg.network.monolith_port}:{port}"]
    else:  # backend 3-tier: только для сбора /metrics с узла, наружу не публикуется
        svc["ports"] = [f"127.0.0.1:{BACKEND_METRICS_PORT}:{port}"]
    return svc


def monolith(cfg: BenchConfig) -> dict[str, Any]:
    return {
        "name": "arch-bench-monolith",
        "services": {
            "postgres": postgres_service(cfg, "../postgres/postgresql.conf"),
            "migrate": migrate_service(cfg),
            "app": app_service(cfg, "monolith", "app", serve_frontend=True, publish=True),
        },
        "volumes": {"pgdata": {}},
    }


def three_tier(cfg: BenchConfig) -> dict[str, Any]:
    fe = cfg.architectures["three_tier"].components["frontend"]
    mem = _mem(fe.mem_mib)
    return {
        "name": "arch-bench-three-tier",
        "services": {
            "postgres": postgres_service(cfg, "../postgres/postgresql.conf"),
            "migrate": migrate_service(cfg),
            "backend": app_service(
                cfg, "three_tier", "backend", serve_frontend=False, publish=False
            ),
            "frontend": {
                "image": cfg.images.frontend,
                "volumes": ["../nginx/three-tier.conf:/etc/nginx/nginx.conf:ro"],
                "ports": [f"{cfg.network.frontend_port}:{cfg.network.frontend_port}"],
                "cpus": str(fe.cpu),
                "mem_limit": mem,
                "mem_reservation": mem,
                "depends_on": ["backend"],
                "ulimits": NOFILE,
                "restart": "unless-stopped",
                "labels": {"arch-bench.role": "request-path"},
            },
        },
        "volumes": {"pgdata": {}},
    }


def monitoring(cfg: BenchConfig) -> dict[str, Any]:
    """Мониторинг узла: вне пути запроса, ресурсы — из инфраструктурного резерва."""
    img = cfg.images
    return {
        "name": "arch-bench-monitoring",
        "services": {
            "prometheus": {
                "image": img.prometheus,
                "network_mode": "host",
                "command": [
                    "--config.file=/etc/prometheus/prometheus.yml",
                    "--storage.tsdb.retention.time=30d",
                    "--web.listen-address=127.0.0.1:9090",
                ],
                "volumes": [
                    "../monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
                    "../../monitoring/k3s-token:/etc/prometheus/k3s-token:ro",
                    "promdata:/prometheus",
                ],
                "cpus": "0.5",
                "mem_limit": "1024m",
                "restart": "unless-stopped",
            },
            "grafana": {
                "image": img.grafana,
                "network_mode": "host",
                "environment": {
                    "GF_SERVER_HTTP_ADDR": "127.0.0.1",
                    "GF_SERVER_HTTP_PORT": "3000",
                    "GF_SECURITY_ADMIN_PASSWORD": "${GRAFANA_ADMIN_PASSWORD:?"
                    "GRAFANA_ADMIN_PASSWORD "
                    "is required (.env)}",
                    "GF_USERS_ALLOW_SIGN_UP": "false",
                    "GF_ANALYTICS_REPORTING_ENABLED": "false",
                },
                "volumes": [
                    "../monitoring/grafana/provisioning:/etc/grafana/provisioning:ro",
                    "../monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro",
                    "grafanadata:/var/lib/grafana",
                ],
                "cpus": "0.25",
                "mem_limit": "512m",
                "restart": "unless-stopped",
            },
            "cadvisor": {
                "image": img.cadvisor,
                "privileged": True,
                "devices": ["/dev/kmsg"],
                "command": [
                    "--housekeeping_interval=5s",
                    "--docker_only=false",
                    "--store_container_labels=true",
                    "--port=8081",
                ],
                "network_mode": "host",
                "volumes": [
                    "/:/rootfs:ro",
                    "/var/run:/var/run:ro",
                    "/sys:/sys:ro",
                    "/var/lib/docker/:/var/lib/docker:ro",
                ],
                # встроенный HEALTHCHECK образа проверяет :8080, а cAdvisor слушает --port=8081
                "healthcheck": {
                    "test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:8081/healthz"],
                    "interval": "30s",
                    "timeout": "3s",
                    "retries": 3,
                },
                "cpus": "0.5",
                "mem_limit": "512m",
                "restart": "unless-stopped",
            },
            "node-exporter": {
                "image": img.node_exporter,
                "network_mode": "host",
                "pid": "host",
                "command": ["--path.rootfs=/host", "--web.listen-address=127.0.0.1:9100"],
                "volumes": ["/:/host:ro,rslave"],
                "cpus": "0.25",
                "mem_limit": "128m",
                "restart": "unless-stopped",
            },
            "postgres-exporter": {
                "image": img.postgres_exporter,
                "network_mode": "host",
                "command": ["--collector.stat_statements", "--web.listen-address=127.0.0.1:9187"],
                "environment": {
                    "DATA_SOURCE_NAME": (
                        f"postgresql://{DB_USER}:"
                        "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
                        f"@127.0.0.1:${{PG_EXPORT_PORT:-5432}}/{DB_NAME}?sslmode=disable"
                    )
                },
                "cpus": "0.25",
                "mem_limit": "128m",
                "restart": "unless-stopped",
            },
        },
        "volumes": {"promdata": {}, "grafanadata": {}},
    }


def root_compose() -> dict[str, Any]:
    """docker compose up -d в корне: монолит (A) — полнофункциональная система локально."""
    return {"include": ["infrastructure/generated/compose/monolith.yml"]}


def dev_override(cfg: BenchConfig) -> dict[str, Any]:
    """docker compose -f docker-compose.yml -f docker-compose.dev.yml up: код из рабочей копии,
    журнал запросов, сборка образа локально."""
    return {
        "services": {
            "app": {
                "build": {"context": ".", "dockerfile": "infrastructure/docker/app.Dockerfile"},
                "environment": {"LOG_REQUESTS": "true", "LOG_LEVEL": "DEBUG"},
                "volumes": ["./apps:/app/apps:ro", "./database:/app/database:ro"],
            },
            "migrate": {
                "build": {"context": ".", "dockerfile": "infrastructure/docker/app.Dockerfile"}
            },
        }
    }


def test_compose(cfg: BenchConfig) -> dict[str, Any]:
    """docker compose -f docker-compose.test.yml run --rm tests: тесты против postgres:16."""
    return {
        "name": "arch-bench-test",
        "services": {
            # эфемерная БД тестов: без пароля (trust), порт наружу не публикуется
            "postgres": {
                "image": cfg.postgres.image,
                "environment": {"POSTGRES_HOST_AUTH_METHOD": "trust", "POSTGRES_USER": "postgres"},
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U postgres"],
                    "interval": "2s",
                    "retries": 30,
                },
            },
            "tests": {
                "build": {"context": ".", "dockerfile": "infrastructure/docker/test.Dockerfile"},
                "environment": {
                    "ARCH_BENCH_TEST_DSN": "postgresql://postgres@postgres:5432/postgres"
                },
                "depends_on": {"postgres": {"condition": "service_healthy"}},
                "command": ["python", "-m", "pytest", "-q"],
            },
        },
    }


def header() -> str:
    return f"# {GENERATED_HEADER}.\n# Не редактировать вручную: python -m infrastructure.render\n"
