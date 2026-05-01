"""Mailpit local SMTP service for the local-dev infra stack."""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


@dataclass
class MessagingResources:
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service


def create_messaging(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
) -> MessagingResources:
    """Deploy Mailpit to capture outbound email in local dev.

    Prevents broken Keycloak email-verification flows during development.
    Web UI is accessible at http://mailpit.local-infra.svc.cluster.local:8025.
    """
    deployment = k8s.apps.v1.Deployment(
        "mailpit",
        metadata={"name": "mailpit", "namespace": "local-infra"},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "mailpit"}},
            "template": {
                "metadata": {"labels": {"app": "mailpit"}},
                "spec": {
                    "containers": [
                        {
                            "name": "mailpit",
                            "image": "axllent/mailpit:latest",
                            "ports": [
                                {"containerPort": 1025, "name": "smtp"},
                                {"containerPort": 8025, "name": "ui"},
                            ],
                            "resources": {
                                "limits": {"memory": "64Mi"},
                            },
                        }
                    ]
                },
            },
        },
        opts=_k8s(parent=local_infra_ns),
    )

    service = k8s.core.v1.Service(
        "mailpit-svc",
        metadata={"name": "mailpit", "namespace": "local-infra"},
        spec={
            "selector": {"app": "mailpit"},
            "ports": [
                {"name": "smtp", "port": 1025, "targetPort": 1025},
                {"name": "ui", "port": 8025, "targetPort": 8025},
            ],
        },
        opts=_k8s(parent=deployment),
    )

    return MessagingResources(deployment=deployment, service=service)
