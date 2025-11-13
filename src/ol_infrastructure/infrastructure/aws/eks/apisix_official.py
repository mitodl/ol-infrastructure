# ruff: noqa: E501, PLR0913
"""Configure and install the Apache APISIX Helm chart.

This module deploys the Apache APISIX chart from https://apache.github.io/apisix-helm-chart.
"""

import textwrap

import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from bridge.lib.magic_numbers import AWS_LOAD_BALANCER_NAME_MAX_LENGTH
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo


def setup_apisix(
    cluster_name: str,
    k8s_provider: kubernetes.Provider,
    operations_namespace: kubernetes.core.v1.Namespace,
    node_groups: list[eks.NodeGroupV2],
    gateway_api_crds,
    stack_info: StackInfo,
    k8s_global_labels: dict[str, str],
    operations_tolerations: list[dict[str, str]],
    versions: dict[str, str],
    eks_config: Config,
    target_vpc,
    aws_config: AWSBase,
    cluster: eks.Cluster,
    lb_controller,
):
    """
    Configure and install the Apache APISIX ingress controller.

    This deploys APISIX using the Apache chart from https://apache.github.io/apisix-helm-chart
    in standalone mode with YAML-based configuration provider and apisix-standalone ingress controller.

    Note: The "worker has not received configuration" message in /status/ready is expected during initial
    startup until the ingress controller pushes the first configuration via the Admin API.

    :param cluster_name: The name of the EKS cluster.
    :param k8s_provider: The Kubernetes provider for Pulumi.
    :param operations_namespace: The operations namespace object.
    :param node_groups: A list of EKS node groups.
    :param gateway_api_crds: The Gateway API CRDs.
    :param stack_info: Information about the current Pulumi stack.
    :param k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    :param operations_tolerations: A list of tolerations for scheduling on operations nodes.
    :param versions: A dictionary of component versions.
    :param eks_config: The EKS configuration object.
    :param target_vpc: The target VPC object.
    :param aws_config: The AWS configuration object.
    :param cluster: The EKS cluster object.
    """
    apisix_domains = eks_config.require_object("apisix_domains")

    session_cookie_name = f"{stack_info.env_suffix}_gateway_session".removeprefix(
        "production"
    ).strip("_")

    # APISIX chart uses a different chart version scheme
    # Chart version 2.12.x contains APISIX 3.14.x
    apisix_chart_version = versions["APISIX_CHART"]

    # Create apache-apisix specific labels that include both global labels
    # and app-specific labels for proper service selector matching
    apisix_labels = {
        **k8s_global_labels,
        "app.kubernetes.io/name": "apache-apisix",
        "app.kubernetes.io/component": "gateway",
    }

    kubernetes.helm.v3.Release(
        f"{cluster_name}-apisix-official-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="apache-apisix",
            version=apisix_chart_version,
            namespace="operations",
            cleanup_on_fail=True,
            chart="apisix",
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://apache.github.io/apisix-helm-chart",
            ),
            values={
                # --- Global/Image Configuration ---
                "image": {
                    "repository": cached_image_uri("apache/apisix"),
                    "pullPolicy": "IfNotPresent",
                },
                # --- Autoscaling ---
                "autoscaling": {
                    "enabled": True,
                    "minReplicas": eks_config.get_int("apisix_min_replicas") or 3,
                    "maxReplicas": eks_config.get_int("apisix_max_replicas") or 5,
                    "targetCPUUtilizationPercentage": 50,
                },
                # --- Pod Configuration ---
                "tolerations": operations_tolerations,
                "readinessProbe": {
                    # Temporarily relax readiness to break chicken-egg problem
                    # Workers need to be in service to receive config via Admin API
                    "initialDelaySeconds": 10,
                    "periodSeconds": 10,
                    "timeoutSeconds": 1,
                    "failureThreshold": 30,  # Allow 5 minutes before marking unhealthy
                },
                "resources": {
                    "requests": {
                        "cpu": "100m",
                        "memory": eks_config.get("apisix_memory") or "400Mi",
                    },
                    "limits": {
                        "memory": eks_config.get("apisix_memory") or "400Mi",
                    },
                },
                # --- Service (LoadBalancer) ---
                "service": {
                    "type": "LoadBalancer",
                    "annotations": {
                        "external-dns.alpha.kubernetes.io/hostname": ",".join(
                            apisix_domains
                        ),
                        "service.beta.kubernetes.io/aws-load-balancer-name": f"{cluster_name}-apache-apisix"[
                            :AWS_LOAD_BALANCER_NAME_MAX_LENGTH
                        ],
                        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                        "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
                        "service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled": "true",
                        "service.beta.kubernetes.io/aws-load-balancer-subnets": target_vpc.apply(
                            lambda tvpc: ",".join(tvpc["k8s_public_subnet_ids"])
                        ),
                        "service.beta.kubernetes.io/aws-load-balancer-additional-resource-tags": ",".join(
                            [f"{k}={v}" for k, v in aws_config.tags.items()]
                        ),
                    },
                    "http": {
                        "enabled": True,
                        "servicePort": 80,
                        "containerPort": 9080,
                    },
                    "tls": {
                        "enabled": True,
                        "servicePort": 443,
                        "containerPort": 9443,
                    },
                    "labelsOverride": apisix_labels,
                },
                # --- APISIX Configuration ---
                "apisix": {
                    "deployment": {
                        "mode": "traditional",
                        "role": "traditional",
                        "role_traditional": {
                            "config_provider": "yaml",
                        },
                    },
                    "admin": {
                        "enabled": True,
                        "type": "ClusterIP",
                        "ip": "0.0.0.0",  # noqa: S104 - ClusterIP service, internal only
                        "port": 9180,
                        "servicePort": 9180,
                        "credentials": {
                            "admin": eks_config.require_secret("apisix_admin_key"),
                            "viewer": eks_config.require_secret("apisix_viewer_key"),
                        },
                        "allow": {
                            "ipList": [
                                "0.0.0.0/0",  # Allow all internal access
                            ],
                        },
                    },
                    "ssl": {
                        "enabled": True,
                        "containerPort": 9443,
                    },
                    "prometheus": {
                        "enabled": True,
                        "containerPort": 9091,
                        "path": "/apisix/prometheus/metrics",
                    },
                    # --- NGINX Configuration (nested under apisix) ---
                    "nginx": {
                        "workerProcesses": "auto",
                        "enableCPUAffinity": True,
                        "workerConnections": "10620",
                        "logs": {
                            "enableAccessLog": True,
                            "accessLog": "/dev/stdout",
                            "accessLogFormat": 'time_local="$time_local" '
                            "body_bytes_sent=$body_bytes_sent "
                            "bytes_sent=$bytes_sent "
                            "client=$remote_addr "
                            "host=$host "
                            "remote_addr=$remote_addr "
                            "request_id=$request_id "
                            "request_length=$request_length "
                            "request_method=$request_method "
                            "request_time=$request_time "
                            "request_uri=$request_uri "
                            "status=$status "
                            "upstream_addr=$upstream_addr "
                            "upstream_connect_time=$upstream_connect_time "
                            "upstream_header_time=$upstream_header_time "
                            "upstream_response_time=$upstream_response_time "
                            "upstream_status=$upstream_status "
                            'http_referer="$http_referer" '
                            'http_user_agent="$http_user_agent" '
                            "method=$request_method "
                            'request="$request"',
                            "accessLogFormatEscape": "default",
                            "errorLog": "/dev/stderr",
                            "errorLogLevel": "warn",
                        },
                        "configurationSnippet": {
                            "main": "",
                            "httpStart": textwrap.dedent(
                                """\
                                client_header_buffer_size 8k;
                                large_client_header_buffers 4 32k;
                                """
                            ),
                            "httpEnd": "",
                            "httpSrv": textwrap.dedent(
                                f"""\
                                set $session_compressor zlib;
                                set $session_name {session_cookie_name};
                                """
                            ),
                            "httpAdmin": "",
                            "stream": "",
                        },
                    },
                },
                # --- Etcd Configuration ---
                "etcd": {
                    "enabled": False,
                },
                # --- Ingress Controller Configuration ---
                "ingress-controller": {
                    "enabled": True,
                    "rbac": {
                        "create": True,
                    },
                    "serviceAccount": {
                        "create": True,
                        "name": "apache-apisix-ingress-controller",
                    },
                    "config": {
                        "provider": {
                            "type": "apisix-standalone",
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
                                        "value": eks_config.require_secret(
                                            "apisix_admin_key"
                                        ),
                                    },
                                },
                            },
                        },
                    },
                    "deployment": {
                        "replicas": 3,  # For high availability and load distribution
                        "tolerations": operations_tolerations,
                        "resources": {
                            "requests": {
                                "cpu": "50m",
                                "memory": "256Mi",
                            },
                            "limits": {
                                "memory": "256Mi",
                            },
                        },
                    },
                },
                # --- Metrics ---
                "metrics": {
                    "serviceMonitor": {
                        "enabled": True,
                        "namespace": "operations",
                        "labels": k8s_global_labels,
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[
                cluster,
                *node_groups,
                operations_namespace,
                gateway_api_crds,
                lb_controller,
            ],
        ),
    )

    # Create GatewayClass resource for Gateway API
    gateway_class = kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-gateway-class",
        api_version="gateway.networking.k8s.io/v1",
        kind="GatewayClass",
        metadata={"name": "apisix"},
        spec={"controllerName": "apisix.apache.org/apisix-ingress-controller"},
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[gateway_api_crds],
        ),
    )

    # Create Gateway resource for Gateway API
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-gateway",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata={"name": "apisix", "namespace": "operations"},
        spec={
            "gatewayClassName": "apisix",
            "listeners": [
                {"name": "http", "protocol": "HTTP", "port": 80},
                {"name": "https", "protocol": "HTTPS", "port": 443},
            ],
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[gateway_api_crds, gateway_class],
        ),
    )
