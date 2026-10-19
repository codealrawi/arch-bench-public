#!/bin/sh
# Запуск компонента приложения. Без аргументов — uvicorn с APP_FACTORY; с аргументами —
# выполняет их (например: python -m database.manage migrate up).
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

: "${APP_FACTORY:?APP_FACTORY is required}"
: "${APP_PORT:?APP_PORT is required}"
: "${APP_WORKERS:?APP_WORKERS is required}"

# каталог метрик Prometheus для нескольких воркеров: очищается при каждом старте
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  rm -rf "${PROMETHEUS_MULTIPROC_DIR}"
  mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
fi

# SIGTERM: uvicorn перестаёт принимать соединения и ждёт текущие запросы до
# GRACEFUL_SHUTDOWN_S, затем lifespan закрывает пул БД (корректное завершение).
exec uvicorn "${APP_FACTORY}" --factory \
  --host 0.0.0.0 --port "${APP_PORT}" --workers "${APP_WORKERS}" \
  --no-access-log --timeout-keep-alive 75 \
  --timeout-graceful-shutdown "${GRACEFUL_SHUTDOWN_S:-25}"
