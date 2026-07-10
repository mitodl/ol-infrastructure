"""cert-manager and APISIX ingress resources for the local-dev infra stack."""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import Output, ResourceOptions


@dataclass
class IngressResources:
    cert_manager: k8s.helm.v3.Release
    apisix: k8s.helm.v3.Release


def create_ingress(
    _k8s: Callable[..., ResourceOptions],
    namespaces: dict[str, k8s.core.v1.Namespace],
    apisix_admin_key: Output,
    apisix_viewer_key: Output,
    cert_manager_version: str,
    apisix_version: str,
) -> IngressResources:
    """Deploy cert-manager and APISIX into the operations namespace."""
    operations_ns = namespaces["operations"]

    cert_manager = k8s.helm.v3.Release(
        "cert-manager",
        k8s.helm.v3.ReleaseArgs(
            name="cert-manager",
            chart="cert-manager",
            version=cert_manager_version,
            namespace="operations",
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo="https://charts.jetstack.io",
            ),
            cleanup_on_fail=True,
            values={
                "crds": {"enabled": True, "keep": True},
                "replicaCount": 1,
                "enableCertificateOwnerRef": True,
                "prometheus": {"enabled": False},
            },
        ),
        opts=_k8s(parent=operations_ns),
    )

    apisix = k8s.helm.v3.Release(
        "apisix",
        k8s.helm.v3.ReleaseArgs(
            name="apache-apisix",
            chart="apisix",
            version=apisix_version,
            namespace="operations",
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo="https://apache.github.io/apisix-helm-chart",
            ),
            cleanup_on_fail=True,
            timeout=600,
            values=Output.all(
                admin=apisix_admin_key,
                viewer=apisix_viewer_key,
            ).apply(
                lambda args: {
                    "service": {
                        "type": "NodePort",
                        "http": {
                            "enabled": True,
                            "servicePort": 80,
                            "containerPort": 9080,
                            "nodePort": 30080,
                        },
                        "tls": {
                            "enabled": True,
                            "servicePort": 443,
                            "containerPort": 9443,
                            "nodePort": 30443,
                        },
                    },
                    "apisix": {
                        "deployment": {
                            # Use traditional mode (same as production): Admin API
                            # backed by YAML storage. The ingress controller pushes
                            # routes via Admin API rather than reading the YAML
                            # directly. Workers show "has not received configuration"
                            # until the ingress controller delivers the first sync —
                            # failureThreshold below gives it time to do so.
                            "mode": "traditional",
                            "role": "traditional",
                            "role_traditional": {
                                "config_provider": "yaml",
                            },
                        },
                        "ssl": {
                            "enabled": True,
                            "listenPort": 9443,
                        },
                        "admin": {
                            "enabled": True,
                            "ip": "0.0.0.0",  # noqa: S104
                            "port": 9180,
                            "servicePort": 9180,
                            "credentials": {
                                "admin": args["admin"],
                                "viewer": args["viewer"],
                            },
                            "allow": {
                                "ipList": ["0.0.0.0/0"],
                            },
                        },
                    },
                    # Disable etcd — not needed with config_provider: yaml
                    "etcd": {
                        "enabled": False,
                    },
                    # Allow 5 min for ingress controller to push first config
                    # before the readiness probe gives up (chicken-egg on startup).
                    "readinessProbe": {
                        "initialDelaySeconds": 10,
                        "periodSeconds": 10,
                        "timeoutSeconds": 1,
                        "failureThreshold": 30,
                    },
                    "ingress-controller": {
                        "enabled": True,
                        "config": {
                            "execADCTimeout": "60s",
                            "provider": {
                                "type": "apisix-standalone",
                                "syncPeriod": "1m",
                                "initSyncDelay": "1m",
                            },
                            "kubernetes": {
                                "ingressClass": "apache-apisix",
                                "enableGatewayAPI": True,
                            },
                        },
                        "apisix": {
                            "adminService": {
                                "name": "apache-apisix-admin",
                                "namespace": "operations",
                                "port": 9180,
                            },
                        },
                        "gatewayProxy": {
                            "createDefault": True,
                            "publishService": "apache-apisix-gateway",
                            "provider": {
                                "type": "ControlPlane",
                                "controlPlane": {
                                    "service": {
                                        "name": "apache-apisix-admin",
                                        "port": 9180,
                                    },
                                    "auth": {
                                        "type": "AdminKey",
                                        "adminKey": {
                                            "value": args["admin"],
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "resources": {
                        # 512Mi OOM-kills APISIX under active OIDC load (JWKS
                        # cache + sessions), crash-looping the gateway and
                        # dropping all app auth. 1Gi gives headroom.
                        "limits": {"memory": "1Gi"},
                    },
                    "autoscaling": {"enabled": False},
                    "replicaCount": 1,
                }
            ),
        ),
        opts=_k8s(parent=operations_ns),
    )

    return IngressResources(cert_manager=cert_manager, apisix=apisix)
