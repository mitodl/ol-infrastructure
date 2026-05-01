# ruff: noqa: E501, PLR0913
"""Configure and install the Apache APISIX Helm chart.

This module deploys the Apache APISIX chart from https://apache.github.io/apisix-helm-chart.
"""

import textwrap
from pathlib import Path

import pulumi_eks as eks
import pulumi_fastly as fastly
import pulumi_kubernetes as kubernetes
from pulumi import Config, InvokeOptions, Output, ResourceOptions

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
    fastly_provider: fastly.Provider,
    vpa_release: kubernetes.helm.v3.Release,
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
    :param lb_controller: The AWS Load Balancer Controller.
    :param fastly_provider: The Fastly provider instance.
    :param vpa_release: The VPA Helm release; ensures VPA CRDs exist before the VPA object is created.
    """
    apisix_domains = eks_config.get_object("apisix_domains") or []

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

    # Get Fastly IP ranges for trusted proxy configuration
    fastly_ips = Output.all(
        fastly.get_fastly_ip_ranges(
            opts=InvokeOptions(provider=fastly_provider)
        ).cidr_blocks,
        fastly.get_fastly_ip_ranges(
            opts=InvokeOptions(provider=fastly_provider)
        ).ipv6_cidr_blocks,
    ).apply(lambda blocks: [*blocks[0], *blocks[1]])

    _error_pages_dir = Path(__file__).resolve().parent / "error_pages"
    _gateway_error_html = (_error_pages_dir / "gateway_error.html").read_text(
        encoding="utf-8"
    )
    _error_pages_configmap_name = "apache-apisix-error-pages"

    error_pages_configmap = kubernetes.core.v1.ConfigMap(
        f"{cluster_name}-apisix-error-pages-configmap",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=_error_pages_configmap_name,
            namespace="operations",
            labels=k8s_global_labels,
        ),
        data={"gateway_error.html": _gateway_error_html},
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
        ),
    )

    apisix_helm_release = kubernetes.helm.v3.Release(
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
                    "trustedAddresses": fastly_ips,
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
                            'request="$request" '
                            "cookie_bytes=$cookie_bytes "
                            "cookie_count=$cookie_count "
                            "oidc_session_bytes=$oidc_session_bytes "
                            "cookie_names=$cookie_names "
                            "cookie_sizes=$cookie_sizes",
                            "accessLogFormatEscape": "default",
                            "errorLog": "/dev/stderr",
                            "errorLogLevel": "warn",
                        },
                        "configurationSnippet": {
                            "main": "",
                            "httpStart": textwrap.dedent(
                                """\
                                client_header_buffer_size 8k;
                                large_client_header_buffers 4 64k;
                                """
                            ),
                            "httpEnd": "",
                            "httpSrv": textwrap.dedent(
                                f"""\
                                set $session_compressor zlib;
                                set $session_name {session_cookie_name};

                                set_by_lua_block $cookie_bytes {{
                                    return #(ngx.var.http_cookie or "")
                                }}
                                set_by_lua_block $cookie_count {{
                                    local cookie = ngx.var.http_cookie or ""
                                    if cookie == "" then return "0" end
                                    local count = 0
                                    for pair in (cookie .. ";"):gmatch("([^;]*);") do
                                        local name = pair:match("^%s*([^=]+)=")
                                        if name and name:match("%S") then
                                            count = count + 1
                                        end
                                    end
                                    return tostring(count)
                                }}
                                set_by_lua_block $oidc_session_bytes {{
                                    local session_name = ngx.var.session_name or ""
                                    if session_name == "" then return "0" end
                                    local all = ngx.var.http_cookie or ""
                                    for pair in (all .. ";"):gmatch("([^;]+);") do
                                        local name, val = pair:match("^%s*([^=]+)=(.*)")
                                        if name then
                                            local trimmed_name = name:match("^%s*(.-)%s*$")
                                            if trimmed_name == session_name then
                                                return tostring(#(trimmed_name .. "=" .. val))
                                            end
                                        end
                                    end
                                    return "0"
                                }}
                                set_by_lua_block $cookie_names {{
                                    local cookie = ngx.var.http_cookie or ""
                                    local names = {{}}
                                    for pair in (cookie .. ";"):gmatch("([^;]+);") do
                                        local name = pair:match("^%s*([^=]+)=")
                                        if name then
                                            name = name:match("^%s*(.-)%s*$")
                                            table.insert(names, name)
                                        end
                                    end
                                    return table.concat(names, ",")
                                }}
                                -- cookie_sizes logs each cookie name with its byte count as name:BYTES
                                -- pairs (e.g. _csrf:64,session_id:4096).  Cookie request headers do
                                -- NOT carry the domain/path the cookie was set on; use host= in the
                                -- surrounding log line to correlate which destination received them.
                                set_by_lua_block $cookie_sizes {{
                                    local cookie = ngx.var.http_cookie or ""
                                    local parts = {{}}
                                    for pair in (cookie .. ";"):gmatch("([^;]+);") do
                                        local name = pair:match("^%s*([^=]+)=")
                                        if name then
                                            local trimmed_name = name:match("^%s*(.-)%s*$")
                                            local trimmed_pair = pair:match("^%s*(.-)%s*$")
                                            table.insert(parts, trimmed_name .. ":" .. tostring(#trimmed_pair))
                                        end
                                    end
                                    return table.concat(parts, ",")
                                }}

                                # Serve a branded error page for gateway-level errors (HTTP 400/431
                                # from oversized request headers, HTTP 500 from OIDC plugin failures,
                                # and other upstream/server errors).  The location is marked
                                # `internal` so it is only reachable via nginx's error_page redirect
                                # and cannot be requested directly by clients.
                                error_page 400 431 500 502 503 504 /gateway_error.html;
                                location = /gateway_error.html {{
                                    root /usr/local/apisix/error-pages;
                                    internal;
                                }}
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
                        "execADCTimeout": "60s",  # Increase from default 15s for large config syncs
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
                # --- Pod Disruption Budget ---
                # Protect against simultaneous evictions during VPA-triggered pod restarts,
                # node drains, and rolling updates. minAvailable=2 guarantees at least 2
                # APISIX pods are always serving while permitting more disruptions as the
                # HPA scales the replica count above the 3-pod floor.
                "podDisruptionBudget": {
                    "enabled": True,
                    "minAvailable": 2,
                },
                # --- Metrics ---
                "metrics": {
                    "serviceMonitor": {
                        "enabled": True,
                        "namespace": "operations",
                        "labels": k8s_global_labels,
                    },
                },
                # --- Custom error pages ---
                # Mount the branded gateway error page ConfigMap into every
                # APISIX pod so nginx can serve it via the error_page directive
                # configured above in configurationSnippet.httpSrv.
                "extraVolumes": [
                    {
                        "name": "apisix-error-pages",
                        "configMap": {"name": _error_pages_configmap_name},
                    }
                ],
                "extraVolumeMounts": [
                    {
                        "name": "apisix-error-pages",
                        "mountPath": "/usr/local/apisix/error-pages",
                        "readOnly": True,
                    }
                ],
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
                error_pages_configmap,
            ],
        ),
    )

    # Create GatewayClass resource for Gateway API
    gateway_class = kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-gateway-class",
        api_version="gateway.networking.k8s.io/v1",
        kind="GatewayClass",
        metadata={"name": "apisix"},
        spec={
            "controllerName": "apisix.apache.org/apisix-ingress-controller",
            "parametersRef": {
                "group": "apisix.apache.org",
                "kind": "GatewayProxy",
                "name": "apache-apisix-config",
                "namespace": "operations",
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[gateway_api_crds],
        ),
    )

    # Create Gateway resource for Gateway API
    apisix_gateway = kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-gateway",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata={
            "name": "apisix",
            "namespace": "operations",
        },
        spec={
            "gatewayClassName": "apisix",
            "listeners": [
                {
                    "name": "http",
                    "protocol": "HTTP",
                    "port": 80,
                    "allowedRoutes": {
                        "namespaces": {
                            "from": "All",
                        }
                    },
                },
                {
                    "name": "https",
                    "protocol": "HTTPS",
                    "port": 443,
                    "allowedRoutes": {
                        "namespaces": {
                            "from": "All",
                        }
                    },
                    # TLS configuration is intentionally omitted here because it is handled
                    # separately by per-application ApisixTls CRDs (see ADR-0003).
                },
            ],
            "infrastructure": {
                "parametersRef": {
                    "group": "apisix.apache.org",
                    "kind": "GatewayProxy",
                    "name": "apache-apisix-config",
                }
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[gateway_api_crds, gateway_class],
        ),
    )

    # Get the APISIX LoadBalancer service to retrieve its hostname
    apisix_gateway_svc = kubernetes.core.v1.Service.get(
        f"{cluster_name}-apisix-gateway-svc",
        id="operations/apache-apisix-gateway",
        opts=ResourceOptions(provider=k8s_provider),
    )

    # Workaround: APISIX ingress controller 2.0.0-rc5 doesn't populate Gateway status.addresses
    # from publishService in GatewayProxy spec. Without status.addresses, external-dns cannot
    # discover the LoadBalancer hostname to create DNS records for HTTPRoutes attached to Gateway.
    #
    # Upstream issue: https://github.com/apache/apisix-ingress-controller/issues/2643
    #
    # Solution: Use external-dns.alpha.kubernetes.io/target annotation on Gateway resource.
    # ExternalDNS checks this annotation first before looking at status.addresses.
    # See: https://github.com/kubernetes-sigs/external-dns/blob/master/docs/sources/gateway-api.md
    #
    # Patch the Gateway with the LoadBalancer hostname annotation
    kubernetes.apiextensions.CustomResourcePatch(
        f"{cluster_name}-gateway-annotation-patch",
        api_version="gateway.networking.k8s.io/v1",
        kind="Gateway",
        metadata=apisix_gateway_svc.status.load_balancer.ingress[0].hostname.apply(
            lambda hostname: {
                "name": "apisix",
                "namespace": "operations",
                "annotations": {
                    "pulumi.com/patchForce": "true",
                    "external-dns.alpha.kubernetes.io/target": hostname,
                },
            }
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=apisix_gateway,
            depends_on=[apisix_gateway, apisix_gateway_svc],
        ),
    )

    # Create a VerticalPodAutoscaler for the APISIX gateway deployment.
    #
    # VPA manages memory sizing only; CPU-based horizontal scaling is handled by the HPA
    # (targetCPUUtilizationPercentage: 50, minReplicas: 3, maxReplicas: 5). Splitting
    # authority this way avoids the known HPA/VPA conflict: VPA adjusting CPU requests
    # would distort the CPU utilization percentage that the HPA observes.
    #
    # updateMode "InPlaceOrRecreate" attempts to resize memory without evicting the pod
    # (K8s 1.33+, VPA 1.6+), falling back to eviction only when an in-place update is
    # not possible. The PodDisruptionBudget (maxUnavailable: 1) ensures at most one pod
    # is disrupted at a time if eviction is needed, protecting gateway availability.
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-apisix-vpa",
        api_version="autoscaling.k8s.io/v1",
        kind="VerticalPodAutoscaler",
        metadata={
            "name": "apache-apisix",
            "namespace": "operations",
        },
        spec={
            "targetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "apache-apisix",
            },
            "updatePolicy": {
                "updateMode": "InPlaceOrRecreate",
            },
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": "apisix",
                        # Only control memory so the HPA retains full authority
                        # over CPU requests and therefore replica count decisions.
                        "controlledResources": ["memory"],
                        "controlledValues": "RequestsAndLimits",
                        "minAllowed": {
                            "memory": eks_config.get("apisix_memory") or "400Mi",
                        },
                        "maxAllowed": {
                            "memory": eks_config.get("apisix_max_memory") or "3Gi",
                        },
                    }
                ]
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            # vpa_release ensures the autoscaling.k8s.io CRDs are installed first.
            # apisix_helm_release ensures the target Deployment exists before VPA targets it.
            depends_on=[vpa_release, apisix_helm_release],
        ),
    )
