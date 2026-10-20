"""Search and document-parsing services for the local-dev infra stack.

Provisions:
  - Qdrant (vector database)
  - OpenSearch (full-text search)
  - Apache Tika (document parsing)
"""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
import requests
import yaml as pyyaml
from pulumi import ResourceOptions

from bridge.lib.versions import (
    OPENSEARCH_CHART_VERSION,
    QDRANT_VERSION,
    TIKA_CHART_VERSION,
)

TIKA_CHART_REPO = "https://apache.jfrog.io/artifactory/tika"


def tika_image_tag() -> str:
    """Return the image tag the deployed Tika chart version runs.

    Deployed stacks install the chart at ``TIKA_CHART_VERSION``, whose
    ``appVersion`` is the image tag (e.g. chart 3.2.2 runs ``3.2.2.0-full``).
    Local-dev keeps its own Deployment, because swapping it for the chart on an
    existing cluster collides with the Deployment and Service already named
    ``tika``, so it reads the tag from the chart index instead of repeating it.
    """
    resp = requests.get(f"{TIKA_CHART_REPO}/index.yaml", timeout=30)
    resp.raise_for_status()
    entries = pyyaml.safe_load(resp.text)["entries"]["tika"]
    (entry,) = (e for e in entries if e["version"] == TIKA_CHART_VERSION)
    return entry["appVersion"]


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
                            # Shared with the Qdrant Cloud clusters
                            # (infrastructure/qdrant_cloud) so local dev runs
                            # the same server MIT Learn talks to in QA and
                            # production, and renovate moves both at once.
                            "image": f"qdrant/qdrant:{QDRANT_VERSION}",
                            # Qdrant mmaps ~30 files per segment and mit-learn
                            # shards its collections 6 ways, so it needs far
                            # more than the 1024-fd soft limit a pod inherits
                            # from the k3d node container. Being a Rust binary
                            # it does not raise its own soft limit the way a
                            # JVM does, so it dies partway through
                            # create_qdrant_collections with "Failed to save
                            # structure on disk with error: Too many open files
                            # (os error 24)". k3d-config.yaml now sets the
                            # cluster-wide default, but raising it here too
                            # means existing clusters are fixed without a
                            # teardown/recreate. `|| true` keeps a lower
                            # inherited hard limit from blocking startup.
                            # ./entrypoint.sh is the image's own entrypoint and
                            # handles SIGTERM/SIGINT plus OOM recovery mode, so
                            # exec into it rather than calling ./qdrant.
                            "command": [
                                "/bin/bash",
                                "-c",
                                # -Sn raises only the soft limit; a bare
                                # `ulimit -n` would also drop the hard limit
                                # from 524288 to 65536, discarding headroom.
                                "ulimit -Sn 65536 2>/dev/null || true; "
                                "exec ./entrypoint.sh",
                            ],
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
            version=OPENSEARCH_CHART_VERSION,
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
                            "image": f"apache/tika:{tika_image_tag()}",
                            "ports": [{"containerPort": 9998}],
                            # The -full image the chart runs adds tesseract,
                            # which Tika calls for scanned PDF pages as a
                            # subprocess outside the JVM heap.
                            "resources": {
                                "limits": {"memory": "1Gi"},
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
