"""Конфигурации nginx.

- three-tier.conf — уровень представления Configuration B (В ПУТИ ЗАПРОСА): статика SPA +
  reverse proxy /api → backend; служебный /metrics backend наружу не публикуется;
- ui-microservices.conf, ui-serverless.conf — только интерфейс для людей в C и D
  (ВНЕ пути запроса k6): статика + проксирование /api на gateway / шлюз OpenFaaS.
"""

from __future__ import annotations

from experiments.config import BenchConfig
from infrastructure.common import GENERATED_HEADER, NAMESPACE, processes_rule

_COMMON_HTTP = """    access_log off;
    sendfile on;
    server_tokens off;
    client_max_body_size 64k;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
"""
_STATIC = """        location / {
            root /usr/share/nginx/html;
            try_files $uri /index.html;
            add_header X-Content-Type-Options nosniff always;
            add_header X-Frame-Options DENY always;
            add_header Referrer-Policy no-referrer always;
            add_header Content-Security-Policy "{CSP}" always;
        }
"""
# та же политика, что отдаёт приложение (apps/shared/http/middleware.py: CSP_FRONTEND)
CSP = (
    "default-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_STATIC = _STATIC.replace("{CSP}", CSP)


def _proxy_block(location: str, target: str, timeout: int, rewrite: str | None = None) -> str:
    rw = f"            rewrite {rewrite} break;\n" if rewrite else ""
    return f"""        location {location} {{
{rw}            proxy_pass {target};
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header X-Request-Id $request_id;
            proxy_read_timeout {timeout}s;
            proxy_send_timeout {timeout}s;
        }}
"""


def _conf(workers: int, upstreams: str, server_body: str, port: int) -> str:
    return f"""# {GENERATED_HEADER} — не редактировать.
worker_processes {workers};
events {{ worker_connections 16384; }}
http {{
{_COMMON_HTTP}{upstreams}
    server {{
        listen {port};
{server_body}    }}
}}
"""


def three_tier(cfg: BenchConfig) -> str:
    fe = cfg.architectures["three_tier"].components["frontend"]
    t = int(cfg.network.upstream_timeout_s)
    upstream = (
        f"    upstream backend {{ server backend:{cfg.network.app_port}; " "keepalive 256; }\n"
    )
    body = (
        _proxy_block("/api/", "http://backend", t)
        + _proxy_block("= /health", "http://backend", t)
        + _proxy_block("= /readiness", "http://backend", t)
        + "        location = /metrics { return 404; }\n"
        + _STATIC
    )
    return _conf(processes_rule(fe.cpu), upstream, body, cfg.network.frontend_port)


def ui_microservices(cfg: BenchConfig) -> str:
    t = int(cfg.network.upstream_timeout_s)
    gw = f"http://gateway.{NAMESPACE}.svc.cluster.local:{cfg.network.app_port}"
    body = _proxy_block("/api/", gw, t) + _STATIC
    return _conf(1, "", body, 8080)


def ui_serverless(cfg: BenchConfig) -> str:
    """/api/v1/<домен>... → /function/<функция>/api/v1/... шлюза OpenFaaS."""
    t = int(cfg.network.upstream_timeout_s)
    gw = f"http://gateway.openfaas.svc.cluster.local:{8080}"
    blocks = []
    for location, fn in (
        ("/api/v1/auth/", "auth"),
        ("/api/v1/users", "auth"),
        ("/api/v1/stats", "stats"),
        ("/api/", "content"),
    ):
        blocks.append(_proxy_block(location, gw, t, rewrite=f"^(.*)$ /function/{fn}$1"))
    return _conf(1, "", "".join(blocks) + _STATIC, 8080)
