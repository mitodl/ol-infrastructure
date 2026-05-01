"""LiteLLM AI proxy for the local-dev infra stack."""

from collections.abc import Callable
from pathlib import Path

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


def create_ai_services(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
    pg_cluster: k8s.apiextensions.CustomResource,
    infra_dir: Path,
) -> k8s.apps.v1.Deployment:
    """Deploy LiteLLM AI proxy into the local-infra namespace.

    Returns the Deployment resource.

    The ``litellm-secrets`` Secret (openai_api_key etc.) is marked optional in
    the pod spec — LiteLLM will start without it.  Create the Secret manually
    or via the Tiltfile if you need live AI features.
    """
    config_cm = k8s.core.v1.ConfigMap(
        "litellm-config",
        metadata={"name": "litellm-config", "namespace": "local-infra"},
        data={
            "config.yaml": (infra_dir / "config" / "litellm-config.yaml").read_text(),
        },
        opts=_k8s(parent=local_infra_ns),
    )

    deployment = k8s.apps.v1.Deployment(
        "litellm",
        metadata={"name": "litellm", "namespace": "local-infra"},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "litellm"}},
            "template": {
                "metadata": {"labels": {"app": "litellm"}},
                "spec": {
                    "containers": [
                        {
                            "name": "litellm",
                            "image": "ghcr.io/berriai/litellm:main-stable",
                            "args": [
                                "--config",
                                "/etc/litellm/config.yaml",
                                "--port",
                                "4000",
                            ],
                            "ports": [{"containerPort": 4000}],
                            "env": [
                                {
                                    "name": "OPENAI_API_KEY",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "litellm-secrets",
                                            "key": "openai_api_key",
                                            "optional": True,
                                        }
                                    },
                                },
                                # Master key must match AI_PROXY_AUTH_TOKEN.
                                {
                                    "name": "LITELLM_MASTER_KEY",
                                    # pragma: allowlist secret
                                    "value": "local-dev-litellm-master-key",
                                },
                                {
                                    "name": "LITELLM_SALT_KEY",
                                    # pragma: allowlist secret
                                    "value": "local-dev-litellm-master-key",
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "name": "config",
                                    "mountPath": "/etc/litellm",
                                    "readOnly": True,
                                }
                            ],
                            "resources": {
                                "limits": {"memory": "2Gi"},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": "litellm-config"}}
                    ],
                },
            },
        },
        opts=_k8s(
            parent=local_infra_ns, depends_on=[d for d in [config_cm, pg_cluster] if d]
        ),
    )

    k8s.core.v1.Service(
        "litellm-svc",
        metadata={"name": "litellm", "namespace": "local-infra"},
        spec={
            "selector": {"app": "litellm"},
            "ports": [{"port": 4000, "targetPort": 4000}],
        },
        opts=_k8s(parent=deployment),
    )

    return deployment
