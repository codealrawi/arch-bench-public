"""Загрузка и проверка ``experiments/configs/bench.yaml``.

Это единственный источник параметров эксперимента.

Проверяются инварианты протокола v1.0 (docs/experimental-protocol.md, docs/resource-allocation.md):
бюджеты CPU/RAM, равенство бюджета пути запроса у всех архитектур, суммарный пул соединений,
сумма долей профиля нагрузки, обязательность SLA_MS.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "experiments" / "configs"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "bench.yaml"
EXPERT_SCORES_PATH = CONFIG_DIR / "expert_scores.yaml"
BUILD_DIR = REPO_ROOT / "infrastructure" / "build"
K6_DIR = REPO_ROOT / "experiments" / "k6"
RAW_DIR = REPO_ROOT / "experiments" / "raw"
RESULTS_DIR = REPO_ROOT / "experiments" / "results"

SLA_REQUIRED_MESSAGE = (
    "SLA_MS is required.\nPlease specify the SLA threshold before running the experiment."
)

ARCHITECTURES = ("monolith", "three_tier", "microservices", "serverless")
# операции профиля нагрузки (M-1) → домен (сервис/функция), обслуживающий операцию
LOAD_OPERATIONS = {
    "feed": "content",
    "get_post": "content",
    "get_profile": "auth",
    "search": "content",
    "create_post": "content",
    "create_comment": "content",
    "reaction": "content",
    "update_profile": "auth",
    "stats": "stats",
}
READ_OPERATIONS = ("feed", "get_post", "get_profile", "search", "stats")
_FLOAT_TOL = 1e-9


class ConfigError(ValueError):
    """Конфигурация нарушает протокол эксперимента."""


class SlaMissingError(ConfigError):
    """SLA_MS не задан ни в конфигурации, ни в окружении."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Budget(_Strict):
    total_vcpu: float = Field(gt=0)
    total_mem_mib: int = Field(gt=0)
    request_path_vcpu: float = Field(gt=0)
    request_path_mem_mib: int = Field(gt=0)
    postgres_vcpu: float = Field(gt=0)
    postgres_mem_mib: int = Field(gt=0)
    infra_reserve_vcpu: float = Field(ge=0)
    infra_reserve_mem_mib: int = Field(ge=0)

    @model_validator(mode="after")
    def _sums(self) -> Budget:
        cpu = self.request_path_vcpu + self.postgres_vcpu + self.infra_reserve_vcpu
        mem = self.request_path_mem_mib + self.postgres_mem_mib + self.infra_reserve_mem_mib
        if cpu > self.total_vcpu + _FLOAT_TOL:
            raise ValueError(f"сумма бюджетов CPU {cpu} > total_vcpu {self.total_vcpu}")
        if mem > self.total_mem_mib:
            raise ValueError(f"сумма бюджетов памяти {mem} > total_mem_mib {self.total_mem_mib}")
        return self


class Postgres(_Strict):
    image: str
    max_connections: int = Field(gt=0)
    total_app_pool: int = Field(gt=0)
    settings: dict[str, str]

    @model_validator(mode="after")
    def _pool(self) -> Postgres:
        if self.total_app_pool >= self.max_connections:
            raise ValueError("total_app_pool должен быть меньше max_connections")
        return self


class Data(_Strict):
    seed: int
    users: int = Field(gt=0)
    moderators: int = Field(ge=0)
    admins: int = Field(ge=0)
    posts: int = Field(gt=0)
    comments: int = Field(ge=0)
    reactions: int = Field(ge=0)
    doctor_share: float = Field(ge=0, le=1)
    user_age_days: int = Field(gt=0)
    created_at_window_days: int = Field(gt=0)
    suspicious_share: float = Field(ge=0, le=1)
    body_words_min: int = Field(gt=0)
    body_words_max: int = Field(gt=0)
    comment_words_min: int = Field(gt=0)
    comment_words_max: int = Field(gt=0)
    reaction_kinds: dict[str, float]
    stats_window_days: int = Field(gt=0)
    feed_limit: int = Field(gt=0)
    comments_page_limit: int = Field(gt=0)
    search_limit: int = Field(gt=0)

    @model_validator(mode="after")
    def _ranges(self) -> Data:
        if self.body_words_min > self.body_words_max:
            raise ValueError("body_words_min > body_words_max")
        if self.comment_words_min > self.comment_words_max:
            raise ValueError("comment_words_min > comment_words_max")
        if self.stats_window_days > self.created_at_window_days:
            raise ValueError("окно /stats больше периода created_at")
        if self.admins + self.moderators >= self.users:
            raise ValueError("admins + moderators должно быть меньше users")
        if self.reactions > self.posts * self.users:
            raise ValueError("reactions больше числа возможных пар (пост, пользователь)")
        if set(self.reaction_kinds) != {"like", "support", "useful"} or not math.isclose(
            sum(self.reaction_kinds.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError("reaction_kinds: like/support/useful с суммой долей 1")
        return self


class Auth(_Strict):
    algorithm: str
    token_ttl_s: int = Field(gt=0)
    pbkdf2_iterations: int = Field(gt=0)
    password_template: str

    @field_validator("algorithm")
    @classmethod
    def _alg(cls, v: str) -> str:
        if v != "HS256":
            raise ValueError("протокол §5 фиксирует HS256")
        return v

    @field_validator("password_template")
    @classmethod
    def _tpl(cls, v: str) -> str:
        if "{user_id}" not in v:
            raise ValueError("password_template должен содержать {user_id}")
        return v


class Load(_Strict):
    executor: str
    steps_server: list[int]
    steps_local: list[int]
    mix: dict[str, float]
    search_terms: list[str]
    think_time_s: float = Field(ge=0)
    ramp_s: int = Field(ge=0)
    warmup_s: int = Field(ge=0)
    measure_s: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
    k6_seed_base: int
    login_pool_size: int = Field(gt=0)
    post_pool_pages: int = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> Load:
        if self.executor != "ramping-vus":
            raise ValueError("протокол P-20 фиксирует ramping-vus")
        for name in ("steps_server", "steps_local"):
            steps = getattr(self, name)
            if not steps or any(s <= 0 for s in steps) or steps != sorted(set(steps)):
                raise ValueError(f"{name}: нужны строго возрастающие положительные значения")
        if set(self.mix) != set(LOAD_OPERATIONS):
            raise ValueError(f"mix должен содержать ровно {sorted(LOAD_OPERATIONS)}")
        if not self.search_terms or any(not t.strip() for t in self.search_terms):
            raise ValueError("search_terms: нужен непустой список непустых строк")
        if any(v < 0 for v in self.mix.values()) or not math.isclose(
            sum(self.mix.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError("доли mix должны быть неотрицательны и в сумме давать 1")
        return self


class Runs(_Strict):
    count: int = Field(gt=0)
    interleave: bool


class Coldstart(_Strict):
    repeats: int = Field(gt=0)
    pause_s: float = Field(ge=0)


class Metrics(_Strict):
    percentile_method: str
    failed_window_s: int = Field(gt=0)
    failed_error_share: float = Field(gt=0, le=1)
    monitoring_step_s: int = Field(gt=0)
    count_transport_errors: bool


class App(_Strict):
    access_token_ttl_s: int = Field(gt=0)
    refresh_token_ttl_s: int = Field(gt=0)
    request_timeout_s: float = Field(gt=0)
    db_command_timeout_s: float = Field(gt=0)
    rate_limit_auth_per_minute: int = Field(gt=0)
    log_level: str
    log_requests: bool
    graceful_shutdown_s: int = Field(gt=0)


class Sla(_Strict):
    sla_ms: float | None = None
    sla_metric: str
    source: str | None = None

    @field_validator("sla_metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        if v != "p95":
            raise ValueError("протокол P-48 фиксирует контроль SLA по p95")
        return v


class Statistics(_Strict):
    bootstrap_b: int = Field(gt=0)
    bootstrap_seed: int
    ci_level: float = Field(gt=0, lt=1)
    alpha: float = Field(gt=0, lt=1)
    compare_steps: list[int]
    littles_law_tolerance: float = Field(gt=0)


class InvalidRun(_Strict):
    generator_cpu_max: float = Field(gt=0, le=1)
    generator_cpu_max_duration_s: int = Field(ge=0)
    max_monitoring_gap_s: int = Field(gt=0)
    max_4xx_share: float = Field(ge=0, le=1)


class Mcda(_Strict):
    reference_step: int = Field(gt=0)
    error_threshold: float = Field(ge=0, le=1)
    fault_tolerance_step: int = Field(gt=0)


class Component(_Strict):
    cpu: float = Field(gt=0)
    mem_mib: int = Field(gt=0)
    min: int = Field(ge=0)
    max: int = Field(ge=1)
    workers: int = Field(ge=0)
    db_pool: int = Field(ge=0)
    on_request_path: bool

    @model_validator(mode="after")
    def _replicas(self) -> Component:
        if self.min > self.max:
            raise ValueError("min > max")
        return self

    @property
    def processes_per_replica(self) -> int:
        # nginx/OpenFaaS gateway: workers=0 (собственный пул процессов, к БД не подключаются)
        return max(self.workers, 1)


class Hpa(_Strict):
    target_cpu: int = Field(gt=0, le=100)
    scale_up_stabilization_s: int = Field(ge=0)
    scale_down_stabilization_s: int = Field(ge=0)


class Entry(_Strict):
    """Точка входа для k6: порт (ссылка на поле network) и префиксы путей доменов.

    Путь операции = route_prefixes[домен] + /api/v1/...; у serverless префикс — маршрут шлюза
    OpenFaaS (/function/<name>), у остальных — пустая строка.
    """

    port_key: str
    route_prefixes: dict[str, str]

    @model_validator(mode="after")
    def _domains(self) -> Entry:
        if set(self.route_prefixes) != {"auth", "content", "stats"}:
            raise ValueError("route_prefixes: ровно auth, content, stats")
        return self


class Architecture(_Strict):
    entry: Entry
    components: dict[str, Component]
    hpa: Hpa | None = None
    scale_to_zero: bool | None = None

    def max_cpu(self) -> float:
        return sum(c.cpu * c.max for c in self.components.values())

    def max_mem_mib(self) -> int:
        return sum(c.mem_mib * c.max for c in self.components.values())

    def max_db_connections(self) -> int:
        return sum(c.db_pool * c.processes_per_replica * c.max for c in self.components.values())


class Images(_Strict):
    app: str
    frontend: str
    nginx: str
    prometheus: str
    grafana: str
    cadvisor: str
    node_exporter: str
    postgres_exporter: str
    kube_state_metrics: str


class Network(_Strict):
    app_port: int = Field(gt=0, lt=65536)
    function_port: int = Field(gt=0, lt=65536)
    gateway_node_port: int = Field(ge=30000, le=32767)
    openfaas_node_port: int = Field(ge=30000, le=32767)
    frontend_port: int = Field(gt=0, lt=65536)
    monolith_port: int = Field(gt=0, lt=65536)
    ui_microservices_node_port: int = Field(ge=30000, le=32767)
    ui_serverless_node_port: int = Field(ge=30000, le=32767)
    upstream_timeout_s: float = Field(gt=0)


class BenchConfig(_Strict):
    protocol_version: str
    budget: Budget
    postgres: Postgres
    images: Images
    network: Network
    app: App
    data: Data
    auth: Auth
    load: Load
    coldstart: Coldstart
    runs: Runs
    metrics: Metrics
    sla: Sla
    statistics: Statistics
    invalid_run: InvalidRun
    mcda: Mcda
    architectures: dict[str, Architecture]

    @model_validator(mode="after")
    def _architectures(self) -> BenchConfig:
        if set(self.architectures) != set(ARCHITECTURES):
            raise ValueError(f"architectures должны быть ровно {ARCHITECTURES}")
        for name, arch in self.architectures.items():
            if arch.max_cpu() > self.budget.request_path_vcpu + _FLOAT_TOL:
                raise ValueError(
                    f"{name}: Σ(max·cpu)={arch.max_cpu()} > бюджета пути запроса "
                    f"{self.budget.request_path_vcpu}"
                )
            if arch.max_mem_mib() > self.budget.request_path_mem_mib:
                raise ValueError(f"{name}: Σ(max·mem) > бюджета памяти пути запроса")
            if arch.max_db_connections() > self.postgres.total_app_pool:
                raise ValueError(
                    f"{name}: суммарный пул {arch.max_db_connections()} > "
                    f"total_app_pool {self.postgres.total_app_pool}"
                )
            scalable = any(c.max > c.min for c in arch.components.values())
            if scalable and arch.hpa is None:
                raise ValueError(f"{name}: есть масштабируемые компоненты, но нет hpa")
        for name, arch in self.architectures.items():
            if arch.entry.port_key not in Network.model_fields:
                raise ValueError(f"{name}: entry.port_key={arch.entry.port_key} нет в network")
        sl = self.architectures["serverless"]
        if sl.scale_to_zero:
            raise ValueError("протокол P-34: в основной серии scale_to_zero=false")
        if any(c.min < 1 for c in self.architectures["serverless"].components.values()):
            raise ValueError("протокол P-34: min >= 1 тёплая реплика")
        for step in self.statistics.compare_steps:
            if step not in self.load.steps_server:
                raise ValueError(f"compare_steps: ступени {step} нет в steps_server")
        return self


def entry_port(cfg: BenchConfig, architecture: str) -> int:
    port = getattr(cfg.network, cfg.architectures[architecture].entry.port_key)
    assert isinstance(port, int)
    return port


def config_sha256(path: Path = DEFAULT_CONFIG_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bench_config(path: Path | str = DEFAULT_CONFIG_PATH) -> BenchConfig:
    """Читает и проверяет конфигурацию. Нарушение протокола → ConfigError."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"файл конфигурации не найден: {p}")
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"повреждённый YAML {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: ожидался YAML-словарь")
    try:
        return BenchConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def resolve_sla_ms(cfg: BenchConfig, env: dict[str, str] | None = None) -> float:
    """Возвращает SLA_MS. Источники: env SLA_MS и/или sla.sla_ms в конфигурации.

    Значения по умолчанию нет (протокол P-48). Если не задан нигде — SlaMissingError;
    если заданы оба и различаются — ConfigError (неоднозначность).
    """
    environ = os.environ if env is None else env
    env_raw = environ.get("SLA_MS")
    env_val: float | None = None
    if env_raw is not None and env_raw.strip() != "":
        try:
            env_val = float(env_raw)
        except ValueError as exc:
            raise ConfigError(f"SLA_MS не число: {env_raw!r}") from exc
    cfg_val = cfg.sla.sla_ms
    if env_val is None and cfg_val is None:
        raise SlaMissingError(SLA_REQUIRED_MESSAGE)
    if env_val is not None and cfg_val is not None and not math.isclose(env_val, cfg_val):
        raise ConfigError(f"SLA_MS из окружения ({env_val}) ≠ sla.sla_ms ({cfg_val})")
    value = env_val if env_val is not None else cfg_val
    assert value is not None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"SLA_MS должен быть положительным конечным числом, получено {value}")
    return value
