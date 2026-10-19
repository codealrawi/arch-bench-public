#!/usr/bin/env bash
# Финальная серия на целевом узле (STAGE 2, final scientific environment):
#   4 конфигурации × 10 прогонов × 8 ступеней (чередование), для каждой ступени:
#   сброс БД к эталону seed → разгон → прогрев → измерение (k6 на ОТДЕЛЬНОЙ машине) → сбор метрик;
#   затем анализ (статистика, таблицы, рисунки) и проверка пригодности для статьи.
#
# Требуется: scripts/install.sh выполнен; .env (JWT_SECRET, POSTGRES_PASSWORD, OPENFAAS_PASSWORD,
# TARGET_HOST, GENERATOR_SSH, PROMETHEUS_URL); git commit без незакоммиченных изменений.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo ".env не найден (см. .env.example)" >&2; exit 1; }
set -a; source .env; set +a
: "${TARGET_HOST:?TARGET_HOST is required}"
: "${GENERATOR_SSH:?GENERATOR_SSH is required (k6 запускается на отдельной машине)}"
: "${PROMETHEUS_URL:?PROMETHEUS_URL is required}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
PY=.venv/bin/python

./scripts/deploy.sh down
$PY -m experiments.orchestrator.run --environment server --target-host "$TARGET_HOST" \
    --prometheus-url "$PROMETHEUS_URL" "$@"
$PY -m experiments.analysis.run_analysis --results experiments/raw --out experiments/results
$PY scripts/validate_results.py --results experiments/raw --out experiments/results \
    --for-article --deep
