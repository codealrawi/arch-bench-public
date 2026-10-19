# Мониторинг

| Что | Источник | Куда |
|---|---|---|
| CPU по контейнерам Docker, CPU узла (`id="/"`) | cAdvisor → Prometheus | `step_*/resources.csv` (`bench/orchestrator/resources.py`) |
| CPU по подам k3s | cAdvisor kubelet → Prometheus | `step_*/resources.csv` |
| Число реплик во времени | `kubectl get deploy` каждые 5 с (`PodsSampler`) | `step_*/pods.csv` |
| Активные соединения PG | `pg_stat_activity` каждые 5 с (`PgConnectionsSampler`) | `step_*/pg_connections.csv` |
| CPU генератора | `scripts/cpu_sampler.py` на машине генератора, 1 с | `step_*/generator_cpu.csv` |

Отклонение от текста протокола v1.0 (P-49): реплики собираются через `kubectl`, а не через
kube-state-metrics. Измеряется та же величина (`status.replicas`, `status.readyReplicas`);
лишний компонент в кластере не нужен. Это отклонение вынесено автору на подтверждение.

## Установка на целевом узле

1. `docker compose -f monitoring/docker-compose.monitoring.yml up -d` (после установки k3s).
2. Проверить: `curl -s 127.0.0.1:9090/api/v1/targets | jq '.data.activeTargets[].health'`.
3. Токен для kubelet:
   ```bash
   kubectl -n kube-system create serviceaccount prometheus-scraper
   kubectl create clusterrolebinding prometheus-scraper --clusterrole=system:kubelet-api-admin \
       --serviceaccount=kube-system:prometheus-scraper
   kubectl -n kube-system create token prometheus-scraper --duration=720h > monitoring/k3s-token
   ```
   `monitoring/k3s-token` — секрет, он в `.gitignore`.
