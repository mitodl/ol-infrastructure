"""Search and document-parsing services for the local-dev infra stack.

Provisions:
  - Qdrant (vector database)
  - OpenSearch (full-text search)
  - Apache Tika (document parsing)
"""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


@dataclass
class SearchResources:
    qdrant: k8s.apps.v1.Deployment
    opensearch: k8s.helm.v3.Release
    tika: k8s.apps.v1.Deployment


def create_search(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
) -> SearchResources:
    """Deploy Qdrant, OpenSearch, and Tika into the local-infra namespace."""
    qdrant = k8s.apps.v1.Deployment(
        "qdrant",
        metadata={"name": "qdrant", "namespace": "local-infra"},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "qdrant"}},
            "template": {
                "metadata": {"labels": {"app": "qdrant"}},
                "spec": {
                    "containers": [
                        {
                            "name": "qdrant",
                            "image": "qdrant/qdrant:v1.12.5",
                            "ports": [
                                {"containerPort": 6333, "name": "http"},
                                {"containerPort": 6334, "name": "grpc"},
                            ],
                            "resources": {
                                "limits": {"memory": "512Mi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "qdrant-storage",
                                    "mountPath": "/qdrant/storage",
                                }
                            ],
                        }
                    ],
                    "volumes": [{"name": "qdrant-storage", "emptyDir": {}}],
                },
            },
        },
        opts=_k8s(parent=local_infra_ns),
    )

    k8s.core.v1.Service(
        "qdrant-svc",
        metadata={"name": "qdrant", "namespace": "local-infra"},
        spec={
            "selector": {"app": "qdrant"},
            "ports": [
                {"name": "http", "port": 6333, "targetPort": 6333},
                {"name": "grpc", "port": 6334, "targetPort": 6334},
            ],
        },
        opts=_k8s(parent=qdrant),
    )

    opensearch = k8s.helm.v3.Release(
        "opensearch",
        k8s.helm.v3.ReleaseArgs(
            name="opensearch",
            chart="opensearch",
            version="3.4.0",
            namespace="local-infra",
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo="https://opensearch-project.github.io/helm-charts",
            ),
            cleanup_on_fail=True,
            timeout=600,
            values={
                "singleNode": True,
                "replicas": 1,
                # Heap matches the known-good mit-learn docker-compose value
                # (-Xmx1024m). 256m tripped the parent circuit breaker at baseline
                # and made `recreate_index` fail. Keep heap ~50% of the container
                # limit so the JVM has room for off-heap/direct memory + OS.
                "opensearchJavaOpts": "-Xms1024m -Xmx1024m",
                "resources": {
                    "limits": {"memory": "2Gi"},
                },
                "persistence": {"size": "5Gi"},
                "config": {"opensearch.yml": "plugins.security.disabled: true\n"},
                "extraEnvs": [
                    {"name": "DISABLE_INSTALL_DEMO_CONFIG", "value": "true"},
                    {"name": "DISABLE_SECURITY_PLUGIN", "value": "true"},
                ],
            },
        ),
        opts=_k8s(parent=local_infra_ns),
    )

    tika = k8s.apps.v1.Deployment(
        "tika",
        metadata={"name": "tika", "namespace": "local-infra"},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "tika"}},
            "template": {
                "metadata": {"labels": {"app": "tika"}},
                "spec": {
                    "containers": [
                        {
                            "name": "tika",
                            "image": "apache/tika:3.0.0.0",
                            "ports": [{"containerPort": 9998}],
                            "resources": {
                                "limits": {"memory": "512Mi"},
                            },
                        }
                    ]
                },
            },
        },
        opts=_k8s(parent=local_infra_ns),
    )

    k8s.core.v1.Service(
        "tika-svc",
        metadata={"name": "tika", "namespace": "local-infra"},
        spec={
            "selector": {"app": "tika"},
            "ports": [{"port": 9998, "targetPort": 9998}],
        },
        opts=_k8s(parent=tika),
    )

    return SearchResources(qdrant=qdrant, opensearch=opensearch, tika=tika)
