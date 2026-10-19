#!/usr/bin/env bash
# Резервная копия БД (pg_dump -Fc) из работающей конфигурации.
#   ./scripts/backup-db.sh [файл]      → backups/arch-bench-<UTC>.dump (по умолчанию)
# Снимается ВНУТРИ контейнера postgres:16 (pg_dump той же версии, что сервер).
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
OUT="${1:-backups/arch-bench-$(date -u +%Y%m%dT%H%M%SZ).dump}"
mkdir -p "$(dirname "$OUT")"
umask 077

if kubectl -n arch-bench get deploy/postgres >/dev/null 2>&1; then
  kubectl -n arch-bench exec deploy/postgres -- pg_dump -U bench -d bench -Fc --no-owner > "$OUT"
else
  CID=$(docker ps --filter "label=com.docker.compose.service=postgres" \
        --filter "name=arch-bench-" -q | head -1)
  [[ -n "$CID" ]] || { echo "работающий PostgreSQL не найден" >&2; exit 1; }
  docker exec "$CID" pg_dump -U bench -d bench -Fc --no-owner > "$OUT"
fi
[[ -s "$OUT" ]] || { echo "пустой файл резервной копии" >&2; exit 1; }
sha256sum "$OUT" > "$OUT.sha256"
echo "OK: $OUT ($(du -h "$OUT" | cut -f1)), sha256 → $OUT.sha256"
