#!/usr/bin/env bash
# Установка платформы на ЧИСТЫЙ VPS Ubuntu 24.04 (Timeweb Cloud): Docker → k3s v1.28 →
# helm → OpenFaaS CE → faas-cli → Python-окружение → образы → мониторинг (токен, exporters).
#
#   sudo ./scripts/install.sh                         # всё
#   sudo ./scripts/install.sh --configure-firewall    # + ufw (SSH, 80/443, порты API — для ADMIN_IPS)
#
# Требования (проверяются): Ubuntu 24.04, ≥ 8 vCPU, ≥ 16 ГБ RAM, ≥ 60 ГБ свободного диска.
# Скрипт идемпотентен: повторный запуск не переустанавливает уже установленное.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
K3S_CHANNEL="v1.28"
CONFIGURE_FIREWALL=0
for arg in "$@"; do
  case "$arg" in
    --configure-firewall) CONFIGURE_FIREWALL=1 ;;
    *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mВНИМАНИЕ: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -f .env ]] || die ".env не найден: cp .env.example .env && nano .env"
set -a; source .env; set +a
TARGET_USER="${SUDO_USER:-root}"

log "1/9 Проверка системы"
. /etc/os-release
[[ "${ID}" == "ubuntu" && "${VERSION_ID}" == "24.04" ]] || warn "проверено на Ubuntu 24.04, у вас ${PRETTY_NAME}"
CPUS=$(nproc); MEM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
echo "CPU: ${CPUS}, RAM: ${MEM_GB} ГБ, свободно на /: ${DISK_GB} ГБ"
(( CPUS >= 8 )) || warn "меньше 8 vCPU: бюджет эксперимента (8 vCPU) не выполняется — результаты несопоставимы"
(( MEM_GB >= 15 )) || warn "меньше 16 ГБ RAM"
(( DISK_GB >= 60 )) || warn "меньше 60 ГБ диска: сырые данные серии ≈ 25–40 ГБ"
for v in JWT_SECRET POSTGRES_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  [[ -n "${!v:-}" ]] || die "в .env не задан $v"
done
(( ${#JWT_SECRET} >= 32 )) || die "JWT_SECRET короче 32 символов"

log "2/9 Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl git jq python3.12 python3.12-venv \
  iputils-ping

log "3/9 Docker"
# Docker ≥ 29 по умолчанию хранит образы в containerd-snapshotter; cAdvisor (v0.49–v0.52) такие
# контейнеры не распознаёт («failed to identify the read-write layer ID») — CPU по контейнерам
# для compose-архитектур не собирался бы. Классический overlay2 — до установки Docker.
install -d /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  echo '{"features": {"containerd-snapshotter": false}}' > /etc/docker/daemon.json
fi
jq -e '.features["containerd-snapshotter"] == false' /etc/docker/daemon.json >/dev/null \
  || die "/etc/docker/daemon.json: нужно \"features\": {\"containerd-snapshotter\": false}"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
[[ "$(docker info --format '{{.Driver}}')" == "overlay2" ]] \
  || die "Docker не на overlay2 (после изменения daemon.json: systemctl restart docker и пересборка образов)"
usermod -aG docker "${TARGET_USER}" || true
docker version --format 'Docker {{.Server.Version}}'
docker compose version

log "4/9 k3s ${K3S_CHANNEL}.x (traefik — только для Ingress UI; API эксперимента — через NodePort)"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL="${K3S_CHANNEL}" \
    sh -s - --disable servicelb --write-kubeconfig-mode 644
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
# API отвечает раньше, чем узел зарегистрирован: kubectl wait --all без узлов падает
until [[ "$(kubectl get nodes --no-headers 2>/dev/null | wc -l)" -gt 0 ]]; do sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=300s
k3s --version | head -1

log "5/9 helm, faas-cli, OpenFaaS CE"
command -v helm >/dev/null || curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
command -v faas-cli >/dev/null || curl -sSL https://cli.openfaas.com | sh
kubectl apply -f https://raw.githubusercontent.com/openfaas/faas-netes/master/namespaces.yml
helm repo add openfaas https://openfaas.github.io/faas-netes/ >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install openfaas openfaas/openfaas -n openfaas \
  -f infrastructure/generated/openfaas/values.yaml --wait --timeout 10m
OPENFAAS_PASSWORD=$(kubectl -n openfaas get secret basic-auth -o jsonpath='{.data.basic-auth-password}' | base64 -d)
if grep -q '^OPENFAAS_PASSWORD=' .env; then
  sed -i "s|^OPENFAAS_PASSWORD=.*|OPENFAAS_PASSWORD=${OPENFAAS_PASSWORD}|" .env
else
  echo "OPENFAAS_PASSWORD=${OPENFAAS_PASSWORD}" >> .env
fi
helm list -n openfaas

log "6/9 Python-окружение (lock-файл с хешами)"
sudo -u "${TARGET_USER}" python3.12 -m venv .venv
sudo -u "${TARGET_USER}" .venv/bin/pip install --quiet --require-hashes -r requirements-tools.lock
sudo -u "${TARGET_USER}" .venv/bin/python -m infrastructure.render --check \
  || die "infrastructure/generated не соответствует bench.yaml"

log "7/9 Образы (один образ приложения для всех архитектур) и импорт в k3s"
APP_IMAGE=$(.venv/bin/python -c "from experiments.config import load_bench_config as l; print(l().images.app)")
FE_IMAGE=$(.venv/bin/python -c "from experiments.config import load_bench_config as l; print(l().images.frontend)")
docker build -f infrastructure/docker/app.Dockerfile -t "${APP_IMAGE}" .
docker build -f infrastructure/docker/frontend.Dockerfile -t "${FE_IMAGE}" .
docker save "${APP_IMAGE}" "${FE_IMAGE}" | k3s ctr images import -
# OpenFaaS CE разворачивает функции только из ПУБЛИЧНЫХ образов: образ приложения с реестром
# в имени (bench.yaml images.app, например docker.io/<user>/arch-bench-app:2.1.0) публикуется.
# Вход в реестр (docker login) выполняет автор; секреты в образ не попадают (только env/Secrets).
if [[ "${APP_IMAGE%%:*}" == */* ]]; then
  docker push "${APP_IMAGE}" \
    || die "docker push ${APP_IMAGE} не удался: выполните docker login для реестра образа"
else
  warn "images.app без реестра (${APP_IMAGE}): конфигурация D (OpenFaaS CE) не развернётся"
fi

log "8/9 Мониторинг: kube-state-metrics, RBAC и токен Prometheus, стек Prometheus/Grafana"
kubectl apply -f infrastructure/generated/kubernetes/monitoring.yaml
for _ in $(seq 1 30); do
  TOKEN=$(kubectl -n kube-system get secret prometheus-scraper-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
  [[ -n "${TOKEN}" ]] && break; sleep 2
done
[[ -n "${TOKEN}" ]] || die "не получен токен prometheus-scraper"
install -m 0600 -o "${TARGET_USER}" /dev/null infrastructure/monitoring/k3s-token
printf '%s' "${TOKEN}" > infrastructure/monitoring/k3s-token
chmod 0644 infrastructure/monitoring/k3s-token  # читается контейнером Prometheus (uid 65534)
docker compose -f infrastructure/generated/compose/monitoring.yml --env-file .env up -d

if (( CONFIGURE_FIREWALL )); then
  log "9/9 Firewall (ufw)"
  [[ -n "${ADMIN_IPS:-}" ]] || die "для --configure-firewall задайте ADMIN_IPS в .env (через пробел)"
  apt-get install -y ufw
  ufw default deny incoming; ufw default allow outgoing
  ufw allow OpenSSH; ufw allow 80/tcp; ufw allow 443/tcp
  for ip in ${ADMIN_IPS}; do
    for port in 8000 8080 30080 30081 30082 31112; do ufw allow from "${ip}" to any port "${port}" proto tcp; done
  done
  ufw --force enable; ufw status verbose
else
  log "9/9 Firewall не настраивался (флаг --configure-firewall); см. docs/TIMEWEB_DEPLOYMENT.md"
fi

chown -R "${TARGET_USER}:" "${REPO}"
log "Готово. Версии:"
docker version --format 'Docker {{.Server.Version}}'; k3s --version | head -1
helm list -n openfaas -o json | jq -r '.[] | "OpenFaaS chart \(.chart) app \(.app_version)"'
faas-cli version --short-version || true
echo "Далее: ./scripts/deploy.sh monolith   (или three-tier | microservices | serverless)"
