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
    apisix_release: k8s.helm.v3.Release,
    tls_secret: k8s.core.v1.Secret,
    mail_hostname: str,
) -> MessagingResources:
    """Deploy Mailpit to capture outbound email in local dev.

    Prevents broken Keycloak email-verification flows during development.
    Web UI is exposed at https://{mail_hostname}.
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

    k8s.apiextensions.CustomResource(
        "mailpit-apisix-route",
        api_version="apisix.apache.org/v2",
        kind="ApisixRoute",
        metadata={"name": "mailpit-route", "namespace": "local-infra"},
        spec={
            "ingressClassName": "apache-apisix",
            "http": [
                {
                    "name": "mailpit",
                    "match": {
                        "hosts": [mail_hostname],
                        "paths": ["/*"],
                    },
                    "backends": [{"serviceName": "mailpit", "servicePort": 8025}],
                }
            ],
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, service]),
    )

    k8s.apiextensions.CustomResource(
        "mailpit-apisix-tls",
        api_version="apisix.apache.org/v2",
        kind="ApisixTls",
        metadata={"name": "mailpit-tls", "namespace": "local-infra"},
        spec={
            "ingressClassName": "apache-apisix",
            "hosts": [mail_hostname],
            "secret": {"name": "local-dev-tls", "namespace": "local-infra"},
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, tls_secret]),
    )

    return MessagingResources(deployment=deployment, service=service)
