"""Kubernetes (k3s v1.28.x): PostgreSQL, миграции, Configuration C, UI, мониторинг кластера."""

from __future__ import annotations

from typing import Any

from experiments.config import BenchConfig, Component, Hpa
from infrastructure.common import (
    DB_NAME,
    DB_USER,
    FACTORIES,
    NAMESPACE,
    NO_DB,
    SECRET_NAME,
    millicpu,
    postgresql_conf,
)
from infrastructure.envs import component_env

KSM_NODE_PORT = 30090
Doc = dict[str, Any]


def resources(cpu: float, mem_mib: int) -> Doc:
    r = {"cpu": millicpu(cpu), "memory": f"{mem_mib}Mi"}
    return {"requests": dict(r), "limits": dict(r)}  # QoS Guaranteed


def namespace(name: str) -> Doc:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}


def secret_env(keys: tuple[str, ...] = ("JWT_SECRET", "DATABASE_URL")) -> list[Doc]:
    """Только нужные компоненту секреты (наименьшие привилегии)."""
    return [
        {"name": k, "valueFrom": {"secretKeyRef": {"name": SECRET_NAME, "key": k}}} for k in keys
    ]


def secrets_for(arch: str, comp: str) -> tuple[str, ...]:
    if (arch, comp) == ("microservices", "moderation"):
        return ()
    if (arch, comp) in NO_DB:
        return ("JWT_SECRET",)
    return ("JWT_SECRET", "DATABASE_URL")


def postgres(cfg: BenchConfig, ns: str = NAMESPACE) -> list[Doc]:
    labels = {"app": "postgres", "arch-bench/role": "database"}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "postgres-conf", "namespace": ns},
            "data": {"postgresql.conf": postgresql_conf(cfg)},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "pgdata", "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "local-path",
                "resources": {"requests": {"storage": "20Gi"}},
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "postgres", "namespace": ns, "labels": labels},
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": "postgres"}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "terminationGracePeriodSeconds": 60,
                        "containers": [
                            {
                                "name": "postgres",
                                "image": cfg.postgres.image,
                                "args": ["-c", "config_file=/etc/postgresql/postgresql.conf"],
                                "env": [
                                    {"name": "POSTGRES_DB", "value": DB_NAME},
                                    {"name": "POSTGRES_USER", "value": DB_USER},
                                    {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                                    {
                                        "name": "POSTGRES_PASSWORD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": SECRET_NAME,
                                                "key": "POSTGRES_PASSWORD",
                                            }
                                        },
                                    },
                                ],
                                "ports": [{"containerPort": 5432}],
                                "resources": resources(
                                    cfg.budget.postgres_vcpu, cfg.budget.postgres_mem_mib
                                ),
                                "readinessProbe": {
                                    "exec": {
                                        "command": ["pg_isready", "-U", DB_USER, "-d", DB_NAME]
                                    },
                                    "periodSeconds": 2,
                                },
                                "livenessProbe": {
                                    "exec": {"command": ["pg_isready", "-U", DB_USER]},
                                    "periodSeconds": 10,
                                    "failureThreshold": 6,
                                },
                                "volumeMounts": [
                                    {"name": "conf", "mountPath": "/etc/postgresql"},
                                    {"name": "data", "mountPath": "/var/lib/postgresql/data"},
                                    {"name": "shm", "mountPath": "/dev/shm"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "conf", "configMap": {"name": "postgres-conf"}},
                            {"name": "data", "persistentVolumeClaim": {"claimName": "pgdata"}},
                            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "postgres", "namespace": ns},
            "spec": {"selector": {"app": "postgres"}, "ports": [{"port": 5432}]},
        },
    ]


def migrate_job(cfg: BenchConfig, ns: str = NAMESPACE) -> Doc:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "migrate", "namespace": ns},
        "spec": {
            "backoffLimit": 6,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "restartPolicy": "OnFailure",
                    "containers": [
                        {
                            "name": "migrate",
                            "image": cfg.images.app,
                            "imagePullPolicy": "IfNotPresent",
                            "args": ["python", "-m", "database.manage", "migrate", "up"],
                            "env": [
                                {
                                    "name": "DATABASE_URL",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": SECRET_NAME, "key": "DATABASE_URL"}
                                    },
                                }
                            ],
                            "resources": resources(0.5, 512),
                        }
                    ],
                }
            },
        },
    }


def hpa(h: Hpa, name: str, ns: str, comp: Component) -> Doc:
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
            "minReplicas": comp.min,
            "maxReplicas": comp.max,
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {"type": "Utilization", "averageUtilization": h.target_cpu},
                    },
                }
            ],
            "behavior": {
                "scaleUp": {"stabilizationWindowSeconds": h.scale_up_stabilization_s},
                "scaleDown": {"stabilizationWindowSeconds": h.scale_down_stabilization_s},
            },
        },
    }


def app_container(
    cfg: BenchConfig,
    name: str,
    image: str,
    env: dict[str, str],
    comp: Component,
    port: int,
    health: str,
    ready: str,
    secret_keys: tuple[str, ...],
) -> Doc:
    probe = {"httpGet": {"path": ready, "port": port}, "periodSeconds": 2, "failureThreshold": 3}
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "env": [{"name": k, "value": v} for k, v in sorted(env.items())] + secret_env(secret_keys),
        "ports": [{"containerPort": port, "name": "http"}],
        "resources": resources(comp.cpu, comp.mem_mib),
        "readinessProbe": probe,
        "livenessProbe": {
            "httpGet": {"path": health, "port": port},
            "periodSeconds": 10,
            "failureThreshold": 6,
            "initialDelaySeconds": 10,
        },
        "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}},
    }


def microservices(cfg: BenchConfig) -> list[Doc]:
    arch = cfg.architectures["microservices"]
    assert arch.hpa is not None
    port = cfg.network.app_port

    def svc_url(n: str) -> str:
        return f"http://{n}.{NAMESPACE}.svc.cluster.local:{port}"

    extra: dict[str, dict[str, str]] = {
        "gateway": {
            "AUTH_URL": svc_url("auth"),
            "CONTENT_URL": svc_url("content"),
            "ANALYTICS_URL": svc_url("analytics"),
        },
        "content": {"MODERATION_URL": svc_url("moderation")},
    }
    docs: list[Doc] = []
    for name, comp in arch.components.items():
        internal = name != "gateway"
        env = component_env(
            cfg,
            comp,
            service=f"microservices-{name}",
            factory=FACTORIES[("microservices", name)],
            port=port,
            uses_db=("microservices", name) not in NO_DB,
            auth_mode="trusted-headers" if internal else "jwt",
            extra={**extra.get(name, {}), **({"TRUST_PROXY": "true"} if internal else {})},
        )
        labels = {
            "app": name,
            "arch-bench/role": "request-path",
            "arch-bench/arch": "microservices",
        }
        docs.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": comp.min,
                    "selector": {"matchLabels": {"app": name}},
                    "template": {
                        "metadata": {
                            "labels": labels,
                            "annotations": {
                                "prometheus.io/scrape": "true",
                                "prometheus.io/port": str(port),
                                "prometheus.io/path": "/metrics",
                            },
                        },
                        "spec": {
                            "terminationGracePeriodSeconds": cfg.app.graceful_shutdown_s + 10,
                            "containers": [
                                app_container(
                                    cfg,
                                    name,
                                    cfg.images.app,
                                    env,
                                    comp,
                                    port,
                                    "/health",
                                    "/readiness",
                                    secrets_for("microservices", name),
                                )
                            ],
                        },
                    },
                },
            }
        )
        svc: Doc = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": port, "targetPort": port, "name": "http"}],
            },
        }
        if name == "gateway":
            svc["spec"]["type"] = "NodePort"
            svc["spec"]["ports"][0]["nodePort"] = cfg.network.gateway_node_port
        docs.append(svc)
        if comp.max > comp.min:
            docs.append(hpa(arch.hpa, name, NAMESPACE, comp))
    return docs


def ui(cfg: BenchConfig, arch: str, node_port: int) -> list[Doc]:
    """Frontend для людей (вне пути запроса k6): ConfigMap с nginx.conf, Deployment, NodePort,
    Ingress traefik (k3s)."""
    from infrastructure import nginx

    conf = nginx.ui_microservices(cfg) if arch == "microservices" else nginx.ui_serverless(cfg)
    name = f"ui-{arch}"
    labels = {"app": name, "arch-bench/role": "ui"}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "data": {"nginx.conf": conf},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "nginx",
                                "image": cfg.images.frontend,
                                "imagePullPolicy": "IfNotPresent",
                                "ports": [{"containerPort": 8080}],
                                "resources": resources(0.1, 128),
                                "readinessProbe": {"httpGet": {"path": "/", "port": 8080}},
                                "volumeMounts": [
                                    {
                                        "name": "conf",
                                        "mountPath": "/etc/nginx/nginx.conf",
                                        "subPath": "nginx.conf",
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "conf", "configMap": {"name": name}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {
                "type": "NodePort",
                "selector": {"app": name},
                "ports": [{"port": 8080, "targetPort": 8080, "nodePort": node_port}],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {
                "ingressClassName": "traefik",
                "rules": [
                    {
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {"name": name, "port": {"number": 8080}}
                                    },
                                }
                            ]
                        }
                    }
                ],
            },
        },
    ]


def kube_state_metrics(cfg: BenchConfig) -> list[Doc]:
    ns, name = "kube-system", "kube-state-metrics"
    rules = [
        {
            "apiGroups": [""],
            "resources": [
                "pods",
                "nodes",
                "namespaces",
                "services",
                "endpoints",
                "persistentvolumeclaims",
            ],
            "verbs": ["list", "watch"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
            "verbs": ["list", "watch"],
        },
        {
            "apiGroups": ["autoscaling"],
            "resources": ["horizontalpodautoscalers"],
            "verbs": ["list", "watch"],
        },
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list", "watch"]},
    ]
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": ns}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": name},
            "rules": rules,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": name},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": name,
            },
            "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": ns}],
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "serviceAccountName": name,
                        "containers": [
                            {
                                "name": name,
                                "image": cfg.images.kube_state_metrics,
                                "ports": [{"containerPort": 8080}],
                                "resources": resources(0.1, 256),
                                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "type": "NodePort",
                "selector": {"app": name},
                "ports": [{"port": 8080, "targetPort": 8080, "nodePort": KSM_NODE_PORT}],
            },
        },
    ]


def prometheus_rbac() -> list[Doc]:
    """ServiceAccount для Prometheus вне кластера: обнаружение подов и cAdvisor kubelet."""
    ns, name = "kube-system", "prometheus-scraper"
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": ns}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": name},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": [
                        "nodes",
                        "nodes/metrics",
                        "nodes/proxy",
                        "pods",
                        "services",
                        "endpoints",
                    ],
                    "verbs": ["get", "list", "watch"],
                },
                {"nonResourceURLs": ["/metrics", "/metrics/cadvisor"], "verbs": ["get"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": name},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": name,
            },
            "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": ns}],
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "kubernetes.io/service-account-token",
            "metadata": {
                "name": f"{name}-token",
                "namespace": ns,
                "annotations": {"kubernetes.io/service-account.name": name},
            },
        },
    ]
