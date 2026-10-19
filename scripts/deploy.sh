#!/usr/bin/env bash
# Развёртывание конфигурации (после scripts/install.sh).
#
#   ./scripts/deploy.sh monolith        # A: app + PostgreSQL (docker compose), UI на :8000
#   ./scripts/deploy.sh three-tier      # B: nginx + app + PostgreSQL, UI на :8080
#   ./scripts/deploy.sh microservices   # C: k3s, gateway :30080, UI :30081 (Ingress traefik :80)
#   ./scripts/deploy.sh serverless      # D: OpenFaaS, шлюз :31112, UI :30082
#   ./scripts/deploy.sh seed            # детерминированный seed в развёрнутую БД
#   ./scripts/deploy.sh down            # остановить всё (данные compose — в volume; k8s — namespace)
#   ./scripts/deploy.sh status
#
# Одновременно работает ОДНА конфигурация (как в эксперименте); deploy останавливает другие.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo ".env не найден (cp .env.example .env)" >&2; exit 1; }
set -a; source .env; set +a
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
PY=.venv/bin/python
G=infrastructure/generated
NS=arch-bench
SECRET=arch-bench-secrets

log() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
compose() { docker compose -f "$G/compose/$1.yml" --env-file .env "${@:2}"; }

secret() {
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
  local tmp; tmp=$(mktemp); trap 'rm -f "$tmp"' RETURN
  chmod 600 "$tmp"
  printf 'POSTGRES_PASSWORD=%s\nJWT_SECRET=%s\nDATABASE_URL=postgresql://bench:%s@postgres.%s.svc.cluster.local:5432/bench\n' \
    "$POSTGRES_PASSWORD" "$JWT_SECRET" "$POSTGRES_PASSWORD" "$NS" > "$tmp"
  kubectl -n "$NS" create secret generic "$SECRET" --from-env-file="$tmp" --dry-run=client -o yaml \
    | kubectl apply -f -
}

k8s_database() {
  secret
  kubectl apply -f "$G/kubernetes/postgres.yaml"
  kubectl -n "$NS" rollout status deployment/postgres --timeout=600s
  kubectl -n "$NS" delete job migrate --ignore-not-found
  kubectl apply -f "$G/kubernetes/migrate-job.yaml"
  kubectl -n "$NS" wait --for=condition=complete job/migrate --timeout=600s
}

stop_others() {
  local keep="$1"
  for c in monolith three-tier; do
    [[ "$c" == "$keep" ]] || compose "$c" down --remove-orphans >/dev/null 2>&1 || true
  done
  if [[ "$keep" != "microservices" && "$keep" != "serverless" ]]; then
    kubectl delete namespace "$NS" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    faas-cli remove -f "$G/openfaas/stack.yml" >/dev/null 2>&1 || true
  fi
  if [[ "$keep" == "microservices" ]]; then
    faas-cli remove -f "$G/openfaas/stack.yml" >/dev/null 2>&1 || true
    kubectl delete -f "$G/openfaas/hpa.yaml" --ignore-not-found >/dev/null 2>&1 || true
  fi
  if [[ "$keep" == "serverless" ]]; then
    kubectl delete -f "$G/kubernetes/microservices.yaml" --ignore-not-found >/dev/null 2>&1 || true
  fi
}

open_db() {  # выставляет DSN, доступный с узла: compose — 127.0.0.1:5432; k8s — ClusterIP
  # (с узла k3s ClusterIP доступен напрямую; port-forward обрывался под нагрузкой)
  if kubectl -n "$NS" get deploy/postgres >/dev/null 2>&1; then
    local ip
    ip=$(kubectl -n "$NS" get svc postgres -o jsonpath='{.spec.clusterIP}')
    DSN="postgresql://bench:${POSTGRES_PASSWORD}@${ip}:5432/bench"
  else
    DSN="postgresql://bench:${POSTGRES_PASSWORD}@127.0.0.1:5432/bench"
  fi
}

case "${1:-}" in
  monolith|three-tier)
    $PY -m infrastructure.render --check
    stop_others "$1"
    log "deploy $1"
    compose "$1" up -d --wait
    compose "$1" ps ;;
  microservices)
    $PY -m infrastructure.render --check
    stop_others microservices
    log "deploy microservices"
    k8s_database
    kubectl apply -f "$G/kubernetes/microservices.yaml" -f "$G/kubernetes/ui-microservices.yaml"
    kubectl -n "$NS" wait --for=condition=Available deployment --all --timeout=600s
    kubectl -n "$NS" get deploy,svc,hpa ;;
  serverless)
    $PY -m infrastructure.render --check
    stop_others serverless
    log "deploy serverless"
    k8s_database
    export OPENFAAS_URL="http://127.0.0.1:31112"
    echo "$OPENFAAS_PASSWORD" | faas-cli login --gateway "$OPENFAAS_URL" --username admin --password-stdin
    faas-cli deploy -f "$G/openfaas/stack.yml"
    kubectl apply -f "$G/openfaas/hpa.yaml" -f "$G/kubernetes/ui-serverless.yaml"
    kubectl -n openfaas-fn wait --for=condition=Available deployment --all --timeout=600s
    faas-cli list --gateway "$OPENFAAS_URL"; kubectl -n openfaas-fn get hpa ;;
  seed)
    log "seed (детерминированный, seed из bench.yaml)"
    open_db
    $PY -m database.manage seed --dsn "$DSN" | jq '{generate_s, write_s, reset_s, counts: .state.counts}' ;;
  down)
    stop_others none ;;
  status)
    for c in monolith three-tier; do compose "$c" ps 2>/dev/null || true; done
    kubectl get pods -A -l 'arch-bench/role' 2>/dev/null || true ;;
  *)
    sed -n '2,13p' "$0"; exit 2 ;;
esac
