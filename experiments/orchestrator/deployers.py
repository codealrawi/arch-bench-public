"""Развёртывание четырёх конфигураций из infrastructure/generated/ (infrastructure/render.py).

Интерфейс одинаков: up → wait_ready → db_dsn → down. DSN к PostgreSQL нужен оркестратору для
сброса БД перед ступенью и сэмплирования соединений; в k8s — ClusterIP сервиса postgres
(с узла k3s), ``kubectl port-forward`` — только если ClusterIP с хоста недоступен.
Миграции: compose — сервис migrate, k8s — Job.
Секреты не попадают в журнал команд (Secret создаётся из временного env-файла,
пароль faas-cli — через stdin).
"""

from __future__ import annotations

import os
import socket
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from subprocess import Popen

import httpx

from experiments.config import BenchConfig, entry_port
from experiments.orchestrator.shell import Shell
from infrastructure.common import DB_NAME, DB_USER, FN_NAMESPACE, NAMESPACE, SECRET_NAME

LOCAL_PG_FORWARD_PORT = 55432
# faas-cli ищет template/<lang>/template.yml относительно ТЕКУЩЕГО каталога (проверено на Timeweb)
FAAS_CWD = Path(__file__).resolve().parents[2] / "serverless"
COMPOSE_FILES = {"monolith": "monolith.yml", "three_tier": "three-tier.yml"}


class DeployError(RuntimeError):
    pass


def _tcp_reachable(host: str, port: int, timeout_s: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise DeployError(f"переменная окружения {name} обязательна (см. .env.example)")
    return v


class Deployer(ABC):
    architecture: str

    def __init__(self, cfg: BenchConfig, generated: Path, shell: Shell, target_host: str):
        self.cfg, self.gen, self.sh, self.target_host = cfg, generated, shell, target_host

    @abstractmethod
    def up(self) -> None: ...

    @abstractmethod
    def down(self) -> None: ...

    @abstractmethod
    def db_dsn(self) -> str: ...

    def entry_base(self) -> str:
        return f"http://{self.target_host}:{entry_port(self.cfg, self.architecture)}"

    def ready_urls(self) -> list[str]:
        prefixes = self.cfg.architectures[self.architecture].entry.route_prefixes
        if self.architecture == "serverless":  # шлюз + каждая функция через шлюз
            return [self.entry_base() + "/healthz"] + [
                f"{self.entry_base()}{p}/_/ready" for p in sorted(set(prefixes.values()))
            ]
        return [self.entry_base() + "/readiness"]

    def wait_ready(self, timeout_s: float = 900) -> None:
        if self.sh.dry_run:
            return
        deadline = time.monotonic() + timeout_s
        pending = self.ready_urls()
        last = ""
        while pending and time.monotonic() < deadline:
            try:
                r = httpx.get(pending[0], timeout=3)
                if r.status_code == 200:
                    pending.pop(0)
                    continue
                last = f"{pending[0]}: HTTP {r.status_code}"
            except httpx.HTTPError as exc:
                last = f"{pending[0]}: {exc}"
            time.sleep(2)
        if pending:
            raise DeployError(f"{self.architecture}: не готов за {timeout_s} с ({last})")


class ComposeDeployer(Deployer):
    def __init__(self, architecture: str, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.architecture = architecture

    def _compose(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.gen / "compose" / COMPOSE_FILES[self.architecture]),
            "--env-file",
            str(Path(".env").resolve()),
            *args,
        ]

    def up(self) -> None:  # миграции — сервис migrate (depends_on: completed_successfully)
        self.sh.run(self._compose("up", "-d", "--force-recreate", "--wait"), timeout=1200)

    def down(self) -> None:
        self.sh.run(self._compose("down", "-v", "--remove-orphans"), timeout=300)

    def db_dsn(self) -> str:
        pw = require_env("POSTGRES_PASSWORD")
        return f"postgresql://{DB_USER}:{pw}@127.0.0.1:5432/{DB_NAME}"


class _K8sBase(Deployer):
    _forward: Popen[bytes] | None = None

    def _secret(self, namespace: str) -> None:
        """Secret из временного env-файла: значения не попадают в журнал команд."""
        pw, jwt = require_env("POSTGRES_PASSWORD"), require_env("JWT_SECRET")
        dsn = f"postgresql://{DB_USER}:{pw}@postgres.{NAMESPACE}.svc.cluster.local:5432/{DB_NAME}"
        self.sh.run(
            ["kubectl", "-n", namespace, "delete", "secret", SECRET_NAME, "--ignore-not-found"]
        )
        fd, tmp = tempfile.mkstemp(prefix="arch-bench-secret-", suffix=".env")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"POSTGRES_PASSWORD={pw}\nJWT_SECRET={jwt}\nDATABASE_URL={dsn}\n")
            self.sh.run(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "create",
                    "secret",
                    "generic",
                    SECRET_NAME,
                    f"--from-env-file={tmp}",
                ]
            )
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _database(self) -> None:
        k = self.gen / "kubernetes"
        self.sh.run(["kubectl", "create", "namespace", NAMESPACE], check=False)
        self._secret(NAMESPACE)
        self.sh.run(["kubectl", "apply", "-f", str(k / "postgres.yaml")])
        self.sh.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "rollout",
                "status",
                "deployment/postgres",
                "--timeout=600s",
            ],
            timeout=660,
        )
        self.sh.run(["kubectl", "-n", NAMESPACE, "delete", "job", "migrate", "--ignore-not-found"])
        self.sh.run(["kubectl", "apply", "-f", str(k / "migrate-job.yaml")])
        self.sh.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "wait",
                "--for=condition=complete",
                "job/migrate",
                "--timeout=600s",
            ],
            timeout=660,
        )

    def _wait_rollout(self, namespace: str) -> None:
        self.sh.run(
            [
                "kubectl",
                "-n",
                namespace,
                "wait",
                "--for=condition=Available",
                "deployment",
                "--all",
                "--timeout=600s",
            ],
            timeout=660,
        )

    def db_dsn(self) -> str:
        """DSN с узла k3s: напрямую на ClusterIP сервиса postgres (kube-proxy на узле).

        port-forward — только запасной путь (k3d на машине разработки, где ClusterIP с хоста
        недоступен): на Timeweb он обрывал соединение сэмплера под нагрузкой (2026-10-05).
        """
        pw = require_env("POSTGRES_PASSWORD")
        if self.sh.dry_run:
            return f"postgresql://{DB_USER}:***@<clusterIP>:5432/{DB_NAME}"
        ip = self.sh.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "get",
                "svc",
                "postgres",
                "-o",
                "jsonpath={.spec.clusterIP}",
            ]
        ).strip()
        if ip and ip != "None" and _tcp_reachable(ip, 5432):
            return f"postgresql://{DB_USER}:{pw}@{ip}:5432/{DB_NAME}"
        if self._forward is None:
            self._forward = self.sh.popen(
                [
                    "kubectl",
                    "-n",
                    NAMESPACE,
                    "port-forward",
                    "svc/postgres",
                    f"{LOCAL_PG_FORWARD_PORT}:5432",
                ]
            )
            time.sleep(3)
        return f"postgresql://{DB_USER}:{pw}@127.0.0.1:{LOCAL_PG_FORWARD_PORT}/{DB_NAME}"

    def _stop_forward(self) -> None:
        if self._forward is not None:
            self._forward.terminate()
            self._forward = None

    def _delete_namespace(self) -> None:
        self.sh.run(
            ["kubectl", "delete", "namespace", NAMESPACE, "--wait=true", "--ignore-not-found"],
            timeout=900,
        )


class MicroservicesDeployer(_K8sBase):
    architecture = "microservices"

    def up(self) -> None:
        self._database()
        self.sh.run(["kubectl", "apply", "-f", str(self.gen / "kubernetes" / "microservices.yaml")])
        self._wait_rollout(NAMESPACE)

    def down(self) -> None:
        self._stop_forward()
        self._delete_namespace()


class ServerlessDeployer(_K8sBase):
    """OpenFaaS CE установлен заранее (scripts/install.sh, helm, generated/openfaas/values.yaml);
    здесь — PG, миграции, функции и HPA."""

    architecture = "serverless"

    def _faas_env(self) -> dict[str, str]:
        return {**os.environ, "OPENFAAS_URL": self.entry_base()}

    def up(self) -> None:
        self._database()
        o = self.gen / "openfaas"
        self.sh.run(
            [
                "faas-cli",
                "login",
                "--gateway",
                self.entry_base(),
                "--username",
                "admin",
                "--password-stdin",
            ],
            stdin_text=require_env("OPENFAAS_PASSWORD"),
        )
        self.sh.run(
            ["faas-cli", "deploy", "-f", str(o / "stack.yml")],
            env=self._faas_env(),
            cwd=FAAS_CWD,
            timeout=900,
        )
        self.sh.run(["kubectl", "apply", "-f", str(o / "hpa.yaml")])
        self._wait_rollout(FN_NAMESPACE)

    def down(self) -> None:
        self._stop_forward()
        o = self.gen / "openfaas"
        self.sh.run(
            ["kubectl", "delete", "-f", str(o / "hpa.yaml"), "--ignore-not-found"], check=False
        )
        self.sh.run(
            ["faas-cli", "remove", "-f", str(o / "stack.yml")],
            env=self._faas_env(),
            cwd=FAAS_CWD,
            check=False,
        )
        self._delete_namespace()


def make_deployer(
    architecture: str, cfg: BenchConfig, generated: Path, shell: Shell, target_host: str
) -> Deployer:
    if architecture in COMPOSE_FILES:
        return ComposeDeployer(architecture, cfg, generated, shell, target_host)
    if architecture == "microservices":
        return MicroservicesDeployer(cfg, generated, shell, target_host)
    if architecture == "serverless":
        return ServerlessDeployer(cfg, generated, shell, target_host)
    raise DeployError(f"неизвестная архитектура {architecture}")
