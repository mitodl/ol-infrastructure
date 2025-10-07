# ruff: noqa: E501, PLR0913, TD002 FIX002
import pulumi_eks as eks
import pulumi_fastly as fastly
import pulumi_kubernetes as kubernetes
from pulumi import Config, InvokeOptions, Output, ResourceOptions

from bridge.lib.magic_numbers import AWS_LOAD_BALANCER_NAME_MAX_LENGTH
from ol_infrastructure.lib.aws.eks_helper import ECR_DOCKERHUB_REGISTRY
from ol_infrastructure.lib.fastly import get_fastly_provider
from ol_infrastructure.lib.ol_types import AWSBase


def setup_traefik(
    cluster_name: str,
    k8s_provider: kubernetes.Provider,
    operations_namespace: kubernetes.core.v1.Namespace,
    node_groups: list[eks.NodeGroupV2],
    prometheus_operator_crds,
    k8s_global_labels: dict[str, str],
    operations_tolerations: list[dict[str, str]],
    versions: dict[str, str],
    eks_config: Config,
    target_vpc,
    aws_config: AWSBase,
    cluster,
):
    """
    Configure and install the Traefik ingress controller.

    :param cluster_name: The name of the EKS cluster.
    :param k8s_provider: The Kubernetes provider for Pulumi.
    :param operations_namespace: The operations namespace object.
    :param node_groups: A list of EKS node groups.
    :param prometheus_operator_crds: The Prometheus Operator CRDs.
    :param k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    :param operations_tolerations: A list of tolerations for scheduling on operations nodes.
    :param versions: A dictionary of component versions.
    :param eks_config: The EKS configuration object.
    :param target_vpc: The target VPC object.
    :param aws_config: The AWS configuration object.
    :param cluster: The EKS cluster object.

    :return: The Gateway API CRDs.
    """
    # The custom resource definitions that come with the traefik helm chart
    # don't install the experimental CRDs even if you say you want to use
    # the experimental features. So we need to install them by hand
    # and explicitly tell the traefik helm release below NOT
    # to install any CRDS or we will get errors.
    #
    # TODO: @Ardiea it would be nice if we could add the k8s_global_labels to these
    gateway_api_crds = kubernetes.yaml.v2.ConfigGroup(
        f"{cluster_name}-gateway-api-experimental-crds",
        files=[
            f"https://github.com/kubernetes-sigs/gateway-api/releases/download/{versions['GATEWAY_API']}/experimental-install.yaml"
        ],
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[cluster],
        ),
    )

    fastly_provider = get_fastly_provider(wrap_in_pulumi_options=False)

    # This helm release installs the traefik k8s gateway api controller
    # which will server as the ingress point for ALL connections going into
    # the applications installed on the cluster. No other publically listening
    # services or load balancers should be configured on the cluster.
    #
    # This does NOT configure a default gateway or any httproutes within
    # the cluster.
    #
    # Ref: https://gateway-api.sigs.k8s.io/reference/spec/
    # Ref: https://doc.traefik.io/traefik/routing/providers/kubernetes-gateway/
    # Ref: https://doc.traefik.io/traefik/providers/kubernetes-gateway/
    #
    # TODO: @Ardiea add the ability to define more ports in config.
    kubernetes.helm.v3.Release(
        f"{cluster_name}-traefik-gateway-controller-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="traefik-gateway-controller",
            chart="traefik",
            version=versions["TRAEFIK_CHART"],
            namespace="operations",
            skip_crds=False,
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://helm.traefik.io/traefik",
            ),
            values={
                "image": {
                    "pullPolicy": "Always",
                    "registry": f"{ECR_DOCKERHUB_REGISTRY}/library",
                },
                "commonLabels": k8s_global_labels,
                "tolerations": operations_tolerations,
                "deployment": {
                    "kind": "Deployment",
                    "podLabels": {
                        # "traffic-gateway-controller-security-group": "True",
                    },
                    "additionalVolumes": [
                        {"name": "plugins"},
                    ],
                },
                "autoscaling": {
                    "enabled": True,
                    "minReplicas": eks_config.get_int("traefik_min_replicas") or 2,
                    "maxReplicas": eks_config.get_int("traefik_max_replicas") or 5,
                    "metrics": [
                        {
                            "resource": {
                                "name": "cpu",
                                "target": {
                                    "type": "Utilization",
                                    "averageUtilization": 50,
                                },
                            },
                            "type": "Resource",
                        }
                    ],
                },
                "additionalVolumeMounts": [
                    {"name": "plugins", "mountPath": "/plugins-storage"},
                ],
                # Not supporting legacy ingress resources
                "kubernetesIngress": {
                    "enabled": False,
                },
                # Do not create a default gateway
                "gateway": {
                    "enabled": False,
                },
                "gatewayClass": {
                    "enabled": True,
                },
                "providers": {
                    "kubernetesGateway": {
                        "enabled": True,
                    },
                },
                # These are important for external-dns to actually work
                "additionalArguments": [
                    "--providers.kubernetesgateway.statusAddress.service.namespace=operations",
                    "--providers.kubernetesgateway.statusAddress.service.name=traefik-gateway-controller",
                    "--serverstransport.insecureskipverify",
                ],
                # Redirect all :80 to :443
                "ports": {
                    "web": {
                        "port": 8000,
                        "expose": {
                            "default": True,
                        },
                        "exposedPort": 80,
                        "redirections": {
                            "entryPoint": {
                                "to": "websecure",
                                "scheme": "https",
                                "permanent": True,
                            }
                        },
                    },
                    "websecure": {
                        "forwardedHeaders": {
                            "trustedIPs": Output.all(
                                fastly.get_fastly_ip_ranges(
                                    opts=InvokeOptions(provider=fastly_provider)
                                ).cidr_blocks,
                                fastly.get_fastly_ip_ranges(
                                    opts=InvokeOptions(provider=fastly_provider)
                                ).ipv6_cidr_blocks,
                            ).apply(lambda blocks: [*blocks[0], *blocks[1]]),
                            "insecure": False,
                        },
                        "port": 8443,
                        "expose": {
                            "default": True,
                        },
                        "exposedPort": 443,
                    },
                },
                "logs": {
                    "general": {
                        "level": "INFO",
                    },
                    "access": {
                        "enabled": True,
                        "format": "json",
                    },
                },
                "resources": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "50Mi",
                    },
                    "limits": {
                        "cpu": "300m",
                        "memory": "150Mi",
                    },
                },
                "metrics": {
                    "prometheus": {
                        "serviceMonitor": {
                            "enabled": True,
                        },
                    },
                },
                "service": {
                    # These control the configuration of the network load balancer that EKS will create
                    # automatically and point at every traefik pod.
                    # Ref: https://kubernetes-sigs.github.io/aws-load-balancer-controller/v2.4/guide/service/annotations/#subnets
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-name": f"{cluster_name}-traefik"[
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
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[
                cluster,
                node_groups[0],
                operations_namespace,
                gateway_api_crds,
                prometheus_operator_crds,
            ],
        ),
    )
    return gateway_api_crds
