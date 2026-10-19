#!/usr/bin/env bash
# Восстановление БД из резервной копии в работающую конфигурацию + проверка.
#   ./scripts/restore-db.sh backups/arch-bench-<UTC>.dump
# Проверяет sha256 копии, восстанавливает в одной транзакции (--single-transaction),
# затем выполняет health (схема = head) и печатает число строк таблиц.
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
IN="${1:?укажите файл резервной копии}"
[[ -f "$IN" ]] || { echo "нет файла $IN" >&2; exit 1; }
if [[ -f "$IN.sha256" ]]; then sha256sum -c "$IN.sha256"; else echo "ВНИМАНИЕ: нет $IN.sha256"; fi
set -a; source .env; set +a
RESTORE=(pg_restore -U bench -d bench --clean --if-exists --no-owner --single-transaction)

if kubectl -n arch-bench get deploy/postgres >/dev/null 2>&1; then
  kubectl -n arch-bench exec -i deploy/postgres -- "${RESTORE[@]}" < "$IN"
  IP=$(kubectl -n arch-bench get svc postgres -o jsonpath='{.spec.clusterIP}')
  DSN="postgresql://bench:${POSTGRES_PASSWORD}@${IP}:5432/bench"
else
  CID=$(docker ps --filter "label=com.docker.compose.service=postgres" \
        --filter "name=arch-bench-" -q | head -1)
  [[ -n "$CID" ]] || { echo "работающий PostgreSQL не найден" >&2; exit 1; }
  docker exec -i "$CID" "${RESTORE[@]}" < "$IN"
  DSN="postgresql://bench:${POSTGRES_PASSWORD}@127.0.0.1:5432/bench"
fi
.venv/bin/python -m database.manage health --dsn "$DSN"
.venv/bin/python -m database.manage state --dsn "$DSN"
echo "OK: восстановлено из $IN"
