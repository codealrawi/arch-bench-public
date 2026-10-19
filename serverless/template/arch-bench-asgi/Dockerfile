# Шаблон OpenFaaS для `faas-cli build` (необязательный путь): собирает тот же образ, что
# infrastructure/docker/app.Dockerfile. В эксперименте функции разворачиваются из уже
# собранного arch-bench-app (stack.yml: skip_build: true) — чтобы образ был идентичен
# образу остальных архитектур.
#
# Контракт OpenFaaS: контейнер слушает 8080, пробы /_/health и /_/ready отвечает само
# ASGI-приложение (serverless/functions/handlers.py), отдельный of-watchdog не нужен.
ARG BASE_IMAGE=arch-bench-app:2.0.0
FROM ${BASE_IMAGE}
ENV APP_PORT=8080 APP_WORKERS=1 HEALTH_PATH=/_/health
EXPOSE 8080
