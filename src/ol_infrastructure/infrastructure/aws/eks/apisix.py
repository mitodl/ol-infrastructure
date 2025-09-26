# ruff: noqa: E501, ERA001, PLR0913
import textwrap

import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions

from bridge.lib.magic_numbers import AWS_LOAD_BALANCER_NAME_MAX_LENGTH
from ol_infrastructure.lib.aws.eks_helper import ECR_DOCKERHUB_REGISTRY
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
    cluster,
):
    """
    Configure and install the APISIX ingress controller.

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
    # At this time 20241218, apisix does not provide first class support for the
    # kubernetes gateway api. So, we are going to use their custom resources and
    # not enable the experimental gateway-api features.
    #
    # We load apisix into the operations namespace for the cluster with a
    # feature flag but we will create the customresources in the application
    # namespaces that need them. See unified-ecommerce as an example.
    #
    # A consequence of this is that apisix will need its own NLB but if
    # we wanted to invest the time we could probably create OLGateway
    # resources that point traefik to the apisix. Seems like one more
    # layer of complexity that we probably don't need just to save a few
    # dollars.

    # Ref: https://apisix.apache.org/docs/ingress-controller/next/tutorials/configure-ingress-with-gateway-api/
    # Ref: https://apisix.apache.org/docs/ingress-controller/getting-started/
    # Ref: https://artifacthub.io/packages/helm/bitnami/apisix
    if eks_config.get_bool("apisix_ingress_enabled"):
        apisix_domains = eks_config.require_object("apisix_domains")
        session_cookie_name = f"{stack_info.env_suffix}_gateway_session".removeprefix(
            "production"
        ).strip("_")
        kubernetes.helm.v3.Release(
            f"{cluster_name}-apisix-gateway-controller-helm-release",
            kubernetes.helm.v3.ReleaseArgs(
                name="apisix",
                version=versions[
                    "APISIX_CHART"
                ],  # Ensure this version exists in Bitnami repo
                namespace="operations",
                # skip_crds=False, # Bitnami charts install CRDs by default
                cleanup_on_fail=True,
                chart="oci://registry-1.docker.io/bitnamicharts/apisix",  # Use Bitnami repo
                values={
                    # --- Global/Common ---
                    # deploymentMode is configured under controlPlane.extraConfig for traditional mode
                    "commonLabels": k8s_global_labels,
                    "image": {
                        "pullPolicy": "Always",
                        # Assuming default Bitnami registry/repository is okay
                        "registry": ECR_DOCKERHUB_REGISTRY,
                    },
                    "global": {
                        "security": {"allowInsecureImages": True},
                        "imageRegistry": ECR_DOCKERHUB_REGISTRY,
                    },
                    "volumePermissions": {
                        "image": {
                            "registry": ECR_DOCKERHUB_REGISTRY,
                        },
                    },
                    # --- Data Plane (Gateway) ---
                    # Disabled for traditional mode
                    "dataPlane": {
                        "enabled": False,
                        "metrics": {
                            "enabled": True,
                            "serviceMonitor": {
                                "enabled": True,
                            },
                        },
                    },
                    # --- Control Plane (Admin API) ---
                    # In traditional mode, this also handles gateway traffic
                    "controlPlane": {
                        "enabled": True,
                        "metrics": {
                            "enabled": True,
                            "serviceMonitor": {
                                "enabled": True,
                            },
                        },
                        "useDaemonSet": False,
                        "autoscaling": {
                            "hpa": {
                                "enabled": True,
                                "minReplicas": eks_config.get("apisix_min_replicas")
                                or "3",
                                "maxReplicas": eks_config.get("apisix_max_replicas")
                                or "5",
                                "targetCPU": "50",
                            },
                        },
                        "pdb": {
                            "create": False
                        },  # No need for pod disruption budget with daemonset
                        "tolerations": operations_tolerations,
                        # Set admin/viewer tokens directly
                        "apiTokenAdmin": eks_config.require("apisix_admin_key"),
                        "apiTokenViewer": eks_config.require("apisix_viewer_key"),
                        # Configure traditional mode
                        "extraConfig": {
                            "deployment": {
                                "role": "traditional",
                                "role_traditional": {
                                    "config_provider": "etcd",  # Default, but explicit
                                },
                            },
                            "nginx_config": {
                                "http": {
                                    "access_log_format": 'time_local="$time_local" '
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
                                },
                                "http_configuration_snippet": textwrap.dedent(
                                    """\
                                    client_header_buffer_size 8k;
                                    large_client_header_buffers 4 32k;
                                    """
                                ),
                                "http_server_configuration_snippet": textwrap.dedent(
                                    f"""\
                                    set $session_compressor zlib;
                                    set $session_name {session_cookie_name};
                                    """
                                ),
                            },
                        },
                        # Note: allow.ipList from original config doesn't map directly.
                        # Access control might need NetworkPolicy or similar.
                        "resources": {  # Default resources seem okay, but let's define explicitly if needed
                            "requests": {
                                "cpu": "100m",
                                "memory": "200Mi",
                            },
                            "limits": {"cpu": "500m", "memory": "400Mi"},
                        },
                        "service": {
                            # Use LoadBalancer for traditional mode as control plane handles traffic
                            "type": "LoadBalancer",
                            "annotations": {
                                "external-dns.alpha.kubernetes.io/hostname": ",".join(
                                    apisix_domains
                                ),
                                "service.beta.kubernetes.io/aws-load-balancer-name": f"{cluster_name}-apisix"[
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
                            # Expose HTTP/HTTPS ports for gateway traffic as per traditional mode docs
                            "extraPorts": [
                                {
                                    "name": "http",
                                    "port": 80,
                                    "targetPort": 9080,  # Default dataPlane HTTP port
                                    "protocol": "TCP",
                                },
                                {
                                    "name": "https",
                                    "port": 443,
                                    "targetPort": 9443,  # Default dataPlane HTTPS port
                                    "protocol": "TCP",
                                },
                            ],
                            # Keep admin API internal (default port 9180 is exposed by chart)
                            # Default metrics port 9099 is also exposed by chart
                        },
                    },
                    # --- Ingress Controller ---
                    # In traditional mode, this still watches K8s resources and configures APISIX via Admin API
                    "ingressController": {
                        "enabled": True,
                        "replicaCount": 2,
                        "tolerations": operations_tolerations,
                        "resources": {  # Apply original gateway resources here
                            "requests": {
                                "cpu": "50m",
                                "memory": "50Mi",
                            },
                            "limits": {
                                "cpu": "50m",
                                "memory": "256Mi",
                            },
                        },
                        # Map controller config under extraConfig
                        "extraConfig": {
                            "apisix": {
                                "service_namespace": "operations",
                                # Use interpolated name for the control plane service
                                "service_name": Output.concat(
                                    "apisix", "-control-plane"
                                ),
                                "admin_key": eks_config.require("apisix_admin_key"),
                                "admin_api_version": "v3",
                            },
                            "kubernetes": {
                                "enable_gateway_api": False,  # As per original config
                                "resync_interval": "1m",
                            },
                        },
                    },
                    # --- Etcd ---
                    "etcd": {
                        "enabled": True,
                        "tolerations": operations_tolerations,
                        "image": {
                            "registry": ECR_DOCKERHUB_REGISTRY,
                        },
                        "persistence": {
                            "enabled": True,
                            "storageClass": "efs-sc",
                        },
                        "livenessProbe": {
                            "enabled": True,
                            "initialDelaySeconds": 120,
                            "timeoutSeconds": 5,
                            "periodSeconds": 10,
                            "successThreshold": 1,
                            "failureThreshold": 3,
                        },
                        "readinessProbe": {
                            "enabled": True,
                            "initialDelaySeconds": 120,
                            "timeoutSeconds": 5,
                            "periodSeconds": 10,
                            "successThreshold": 1,
                            "failureThreshold": 3,
                        },
                        "resources": {
                            "requests": {
                                "cpu": "50m",
                                "memory": "100Mi",
                            },
                            "limits": {
                                "cpu": "100m",
                                "memory": "300Mi",
                            },
                        },
                        # Add auth config if needed based on etcd subchart values
                    },
                    # --- Dashboard (Disable if not needed, seems disabled in original via config structure) ---
                    "dashboard": {
                        "enabled": False,
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
                    gateway_api_crds,  # Keep dependency on Gateway CRDs if still relevant elsewhere
                ],
            ),
        )
