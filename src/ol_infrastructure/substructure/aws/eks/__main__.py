# ruff: noqa: E501

import os
import textwrap
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_KEDA_PORT,
)
from bridge.lib.versions import (
    KEDA_CHART_VERSION,
    KUBE_STATE_METRICS_CHART_VERSION,
    VANTAGE_K8S_AGENT_CHART_VERSION,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import default_psg_egress_args
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.substructure.aws.eks.karpenter import setup_karpenter

env_config = Config("environment")

aws_account = aws.get_caller_identity()

VERSIONS = {
    "VANTAGE_K8S_AGENT_VERSION": os.environ.get(
        "VANTAGE_K8S_AGENT_CHART_VERSION", VANTAGE_K8S_AGENT_CHART_VERSION
    ),
    "KUBE_STATE_METRICS_VERSION": os.environ.get(
        "KUBE_STATE_METRICS_CHART_VERSION", KUBE_STATE_METRICS_CHART_VERSION
    ),
}

stack_info = parse_stack()

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")

target_vpc = network_stack.require_output(env_config.require("target_vpc"))

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={
        "OU": env_config.get("business_unit") or "operations",
        "Environment": cluster_name,
        "Owner": "platform-engineering",
    },
)

k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
    "ol.mit.edu/stack": stack_info.full_name,
}

setup_vault_provider(stack_info)
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster_stack.require_output("kube_config"),
)

############################################################
# Secondary resources for vault-secrets-operator
############################################################
vault_traefik_policy_name = f"{stack_info.env_prefix}-eks-traefik"
vault_traefik_policy = vault.Policy(
    f"{cluster_name}-eks-vault-traefik-policy",
    name=vault_traefik_policy_name,
    policy=Path(__file__).parent.joinpath("operations_vault_policy.hcl").read_text(),
)
vault_traefik_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"{cluster_name}-traefik-gateway-vault-auth-backend-role",
    role_name="traefik-gateway",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=["operations"],
    token_policies=[vault_traefik_policy_name],
)

operations_vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="operations",
    namespace="operations",
    labels=k8s_global_labels,
    vault_address=f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=vault_traefik_auth_backend_role.role_name,
)

operations_vault_k8s_resources = OLVaultK8SResources(
    resource_config=operations_vault_k8s_resources_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
star_odl_mit_edu_secret_name = (
    "odl-wildcard-cert"  # pragma: allowlist secret #  noqa: S105
)
star_odl_mit_edu_static_secret_config = OLVaultK8SStaticSecretConfig(
    name="vault-kv-global-odl-wildcard",
    namespace="operations",
    labels=k8s_global_labels,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=star_odl_mit_edu_secret_name,
    dest_secret_type="kubernetes.io/tls",  # noqa: S106  # pragma: allowlist secret
    mount="secret-global",
    mount_type="kv-v2",
    path="odl-wildcard",
    templates={
        "tls.key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "tls.crt": '{{ get .Secrets "cert_with_proper_newlines" }}',
        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
        "key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "cert": '{{ get .Secrets "cert_with_proper_newlines" }}',
    },
    refresh_after="1h",
    vaultauth=operations_vault_k8s_resources.auth_name,
)
star_odl_mit_edu_static_secret = OLVaultK8SSecret(
    f"{cluster_name}-odl-mit-edu-wildcard-static-secret",
    resource_config=star_odl_mit_edu_static_secret_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
export("star_odl_mit_edu_secret_name", star_odl_mit_edu_secret_name)
export("star_odl_mit_edu_secret_namespace", "operations")

star_ol_mit_edu_secret_name = (
    "ol-wildcard-cert"  # pragma: allowlist secret #  noqa: S105
)
star_ol_mit_edu_static_secret_config = OLVaultK8SStaticSecretConfig(
    name="vault-kv-global-ol-wildcard",
    namespace="operations",
    labels=k8s_global_labels,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=star_ol_mit_edu_secret_name,
    dest_secret_type="kubernetes.io/tls",  # noqa: S106  # pragma: allowlist secret
    mount="secret-global",
    mount_type="kv-v2",
    path="ol-wildcard",
    templates={
        "tls.key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "tls.crt": '{{ get .Secrets "cert_with_proper_newlines" }}',
        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
        "key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "cert": '{{ get .Secrets "cert_with_proper_newlines" }}',
    },
    refresh_after="1h",
    vaultauth=operations_vault_k8s_resources.auth_name,
)
star_ol_mit_edu_static_secret = OLVaultK8SSecret(
    f"{cluster_name}-ol-mit-edu-wildcard-static-secret",
    resource_config=star_ol_mit_edu_static_secret_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
export("star_ol_mit_edu_secret_name", star_ol_mit_edu_secret_name)
export("star_ol_mit_edu_secret_namespace", "operations")


############################################################
# Secondary resources for cert-manager
############################################################

# ClusterIssuer resources to provide a shared, preconfigured method
# for requesting certificates from letsencrypt
cert_manager_clusterissuer_resources = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-cert-manager-clusterissuer-resources",
    skip_await=True,
    objs=[
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {
                "name": "letsencrypt-staging",
                "labels": k8s_global_labels,
            },
            "spec": {
                "acme": {
                    "email": "odl-devops@mit.edu",
                    "server": "https://acme-staging-v02.api.letsencrypt.org/directory",
                    "disableAccountKeyGeneration": False,
                    "privateKeySecretRef": {
                        "name": "letsencrypt-staging-private-key",
                    },
                    "solvers": [
                        {
                            "selector": {
                                "dnsZones": cluster_stack.require_output(
                                    "allowed_dns_zones"
                                ),
                            },
                            "dns01": {
                                "route53": {},
                            },
                        },
                    ],
                },
            },
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {
                "name": "letsencrypt-production",
                "labels": k8s_global_labels,
            },
            "spec": {
                "acme": {
                    "email": "odl-devops@mit.edu",
                    "server": "https://acme-v02.api.letsencrypt.org/directory",
                    "disableAccountKeyGeneration": False,
                    "privateKeySecretRef": {
                        "name": "letsencrypt-production-private-key",
                    },
                    "solvers": [
                        {
                            "selector": {
                                "dnsZones": cluster_stack.require_output(
                                    "allowed_dns_zones"
                                ),
                            },
                            "dns01": {
                                "route53": {},
                            },
                        },
                    ],
                },
            },
        },
    ],
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)

############################################################
# Install the vantage k8s agent
############################################################
# Requires EBS storage class and creates a statefulset
if cluster_stack.require_output("has_ebs_storage"):
    vault_vantage_policy_name = f"{stack_info.env_prefix}-eks-vantage"
    vault_vantage_policy = vault.Policy(
        f"{cluster_name}-eks-vault-vantage-policy",
        name=vault_vantage_policy_name,
        policy=Path(__file__).parent.joinpath("vantage_vault_policy.hcl").read_text(),
    )
    vault_vantage_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"{cluster_name}-vantage-agent-vault-auth-backend-role",
        role_name="vantage-agent",
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=["operations"],
        token_policies=[vault_vantage_policy_name],
    )

    vault_vantage_k8s_resources_config = OLVaultK8SResourcesConfig(
        application_name="vantage-agent",
        namespace="operations",
        labels=k8s_global_labels,
        vault_address=f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=vault_vantage_auth_backend_role.role_name,
    )

    vault_vantage_k8s_resources = OLVaultK8SResources(
        resource_config=vault_vantage_k8s_resources_config,
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
            depends_on=[vault_vantage_auth_backend_role],
        ),
    )
    vantage_api_token_secret_name = "vantage-api-token"  # noqa: S105  # pragma: allowlist secret
    vantage_api_token_secret_config = OLVaultK8SStaticSecretConfig(
        name="vault-kv-global-vantage-api-token",
        namespace="operations",
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=vantage_api_token_secret_name,
        mount="secret-global",
        mount_type="kv-v2",
        path="vantage",
        templates={
            "token": '{{ get .Secrets "token" }}',
        },
        refresh_after="1h",
        vaultauth=vault_vantage_k8s_resources.auth_name,
    )

    vantage_api_token_secret = OLVaultK8SSecret(
        f"{cluster_name}-vantage-api-token-static-secret",
        resource_config=vantage_api_token_secret_config,
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
        ),
    )

    vantage_k8s_agent_release = kubernetes.helm.v3.Release(
        f"{cluster_name}-vantage-k8s-agent-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="vantage-kubernetes-agent",
            chart="vantage-kubernetes-agent",
            version=VERSIONS["VANTAGE_K8S_AGENT_VERSION"],
            namespace="operations",
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://vantage-sh.github.io/helm-charts",
            ),
            values={
                "agent": {
                    "secret": {
                        "name": vantage_api_token_secret_name,
                        "key": "token",
                    },
                    "clusterID": cluster_name,
                    "disableKubeTLSverify": True,
                    "nodeAddressTypes": "InternalIP",
                    "collectNamespaceLabels": "true",
                },
                "persist": {
                    "storageClassName": cluster_stack.require_output(
                        "ebs_storageclass"
                    ),
                },
                "resources": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "100Mi",
                    },
                    "limits": {
                        "cpu": "100m",
                        "memory": "100Mi",
                    },
                },
            },
            skip_await=True,
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[vantage_api_token_secret],
        ),
    )

############################################################
# Install Grafana-Alloy for log and metric collection
############################################################
alloy_env_vars_secret_name = "alloy-env-vars"  # pragma: allowlist secret #  noqa: S105
alloy_env_vars_static_secret_config = OLVaultK8SStaticSecretConfig(
    name=alloy_env_vars_secret_name,
    namespace="operations",
    labels=k8s_global_labels,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=alloy_env_vars_secret_name,
    mount="secret-global",
    mount_type="kv-v2",
    path="grafana",
    restart_target_kind="DaemonSet",
    restart_target_name="grafana-alloy",
    templates={
        "GRAFANA_CLOUD_LOKI_URL": '{{ get .Secrets "loki_endpoint" }}',
        "GRAFANA_CLOUD_LOKI_PASSWORD": '{{ get .Secrets "loki_api_key" }}',
        "GRAFANA_CLOUD_LOKI_USERNAME": '{{ get .Secrets "loki_user_id" }}',
        "GRAFANA_CLOUD_PROMETHEUS_URL": '{{ get .Secrets "prometheus_endpoint" }}',
        "GRAFANA_CLOUD_PROMETHEUS_PASSWORD": '{{ get .Secrets "prometheus_api_key" }}',
        "GRAFANA_CLOUD_PROMETHEUS_USERNAME": '{{ get .Secrets "prometheus_user_id" }}',
        "GRAFANA_CLOUD_TEMPO_URL": '{{ get .Secrets "tempo_endpoint" }}',
        "GRAFANA_CLOUD_TEMPO_PASSWORD": '{{ get .Secrets "tempo_api_key" }}',
        "GRAFANA_CLOUD_TEMPO_USERNAME": '{{ get .Secrets "tempo_user_id" }}',
    },
    refresh_after="1m",
    vaultauth=operations_vault_k8s_resources.auth_name,
)

alloy_env_vars_static_secret = OLVaultK8SSecret(
    f"{cluster_name}-star-odl-mit-edu-static-secret",
    resource_config=alloy_env_vars_static_secret_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=operations_vault_k8s_resources,
        delete_before_replace=True,
    ),
)

alloy_configmap_name = "alloy-config"
alloy_configmap = kubernetes.core.v1.ConfigMap(
    f"{cluster_name}-grafana-alloy-configmap",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=alloy_configmap_name,
        namespace="operations",
        labels=k8s_global_labels,
    ),
    immutable=False,
    data={
        "config.alloy": textwrap.dedent(
            f"""
            // ----------------------------------------------------------
            // General configuration of loki
            logging {{
              level = "info"
              format = "logfmt"
            }}

            // ----------------------------------------------------------
            // Discover servicemonitor and podmonitor resources in the
            // cluster and ship their metrics to prometheus @ grafana cloud
            prometheus.remote_write "publish_to_grafana" {{
              endpoint {{
                url = env("GRAFANA_CLOUD_PROMETHEUS_URL")
                basic_auth {{
                  username = env("GRAFANA_CLOUD_PROMETHEUS_USERNAME")
                  password = env("GRAFANA_CLOUD_PROMETHEUS_PASSWORD")
                }}
                write_relabel_config {{
                  source_labels = ["__name__"]
                  regex         = "go_.*"
                  action        = "drop"
                }}
                write_relabel_config {{
                    source_labels = ["exported_namespace", "exported_container"]
                    regex         = "jupyter;(binder|chp|dind|image-cleaner-dind|pause|kube-scheduler)"
                    action        = "drop"
                }}

                // Collapse some labels that include UUIDs and are not useful
                write_relabel_config {{
                    source_labels = ["__name__"]
                    regex         = "(kube_pod_container_info|kube_pod_container_status_restarts_total)"
                    action        = "replace"
                    target_label  = "uid"
                    replacement   = ""
                }}
                write_relabel_config {{
                    source_labels = ["__name__"]
                    regex         = "kube_pod_container_info"
                    action        = "replace"
                    target_label  = "image_spec"
                    replacement   = ""
                }}
                write_relabel_config {{
                    source_labels = ["__name__"]
                    regex         = "(kube_pod_container_info|kube_pod_container_status_restarts_total)"
                    action        = "replace"
                    target_label  = "exported_pod"
                    replacement   = ""
                }}
                write_relabel_config {{
                    source_labels = ["__name__"]
                    regex         = "kube_pod_container_info"
                    action        = "replace"
                    target_label  = "image_id"
                    replacement   = ""
                }}
                write_relabel_config {{
                    source_labels = ["__name__"]
                    regex         = "kube_pod_container_info"
                    action        = "replace"
                    target_label  = "container_id"
                    replacement   = ""
                }}
              }}
            }}

            prometheus.operator.servicemonitors "servicemonitors" {{
              forward_to = [prometheus.remote_write.publish_to_grafana.receiver]
              // Drop metrics that start with go_
              // These are usually metrics about the exporter itself rather than
              // the application / service we are interested in.

              // Add cluster label
              rule {{
                target_label = "cluster"
                replacement  = "{cluster_name}"
                action       = "replace"
              }}
            }}

            // ----------------------------------------------------------
            // Collect all pod logs and cluster events and ship to loki
            // @ grafana cloud


            discovery.kubernetes "pods" {{
              role = "pod"
            }}

            discovery.relabel "pods" {{
              targets = discovery.kubernetes.pods.targets
              // OL Standard Stuff (application, service, environment)
              // We're going to use the namespace as 'application'
              rule {{
                source_labels = ["__meta_kubernetes_namespace"]
                action = "replace"
                target_label = "application"
              }}
              rule {{
                source_labels = ["application"]
                action = "lowercase"
                target_label = "application"
              }}

            // Select a pod label -> service
              rule {{
                source_labels = [
                    "__meta_kubernetes_pod_label_ol_mit_edu_service",
                    "__meta_kubernetes_pod_label_ol_mit_edu_component",
                    "__meta_kubernetes_pod_label_app_kubernetes_io_component",
                    "__meta_kubernetes_pod_label_app_kubernetes_io_name",
                    "__meta_kubernetes_pod_label_app_kubernetes_io_instance",
                ]
                replacement = "$1"
                regex = ";*([^;]+).*"
                action = "replace"
                target_label = "service"
              }}
              rule {{
                source_labels = ["service"]
                action = "lowercase"
                target_label = "service"
              }}

              rule {{
                source_labels = ["__meta_kubernetes_namespace"]
                action = "replace"
                target_label = "environment"
                replacement = "$1-{stack_info.env_suffix}"
              }}
              rule {{
                source_labels = ["environment"]
                action = "lowercase"
                target_label = "environment"
              }}

              // Extras
              rule {{
                source_labels = ["__meta_kubernetes_namespace"]
                action = "replace"
                target_label = "namespace"
              }}
              rule {{
                source_labels = ["namespace"]
                action = "lowercase"
                target_label = "namespace"
              }}

              rule {{
                source_labels = ["__meta_kubernetes_pod_container_name"]
                action = "replace"
                target_label = "container"
              }}
              rule {{
                source_labels = ["container"]
                action = "lowercase"
                target_label = "container"
              }}

                // Add a pod name label for easier searching / troubleshooting
              rule {{
                source_labels = ["__meta_kubernetes_pod_name"]
                action = "replace"
                target_label = "pod"
              }}

                // Select k8s label -> stack label
                // From least desirable to most desireable
              rule {{
                source_labels = [
                    "__meta_kubernetes_pod_label_ol_mit_edu_stack",
                    "__meta_kubernetes_pod_label_pulumi_stack",
                ]
                replacement = "$1"
                regex = ";*([^;]+).*"
                action = "replace"
                target_label = "stack"
              }}
              // Intentionally not doing a lowercase on stack
            }}

            loki.source.kubernetes "pod_logs" {{
              targets = discovery.relabel.pods.output
              forward_to = [loki.process.pod_logs.receiver]
            }}

            loki.process "pod_logs" {{
              stage.static_labels {{
                values = {{
                  cluster = "{cluster_name}",
                }}
              }}

              stage.label_keep {{
                values = ["application", "cluster", "container", "environment", "namespace", "service", "stack", "pod"]
              }}
              forward_to = [loki.write.publish_to_grafana.receiver]
            }}

            loki.source.kubernetes_events "cluster_events" {{
              job_name   = "integrations/kubernetes/eventhandler"
              log_format = "json"
              forward_to = [loki.process.cluster_events.receiver]
            }}

            loki.process "cluster_events" {{
              stage.static_labels {{
                values = {{
                  cluster = "{cluster_name}",
                  service = "kubernetes-events",
                  application = "eks",
                  environment = "{cluster_name}",
                }}
              }}

              stage.label_keep {{
                values = ["application", "cluster", "environment", "namespace", "service"]
              }}
              forward_to = [loki.write.publish_to_grafana.receiver]
            }}

            loki.write "publish_to_grafana" {{
              endpoint {{
                url = env("GRAFANA_CLOUD_LOKI_URL")
                basic_auth {{
                  username = env("GRAFANA_CLOUD_LOKI_USERNAME")
                  password = env("GRAFANA_CLOUD_LOKI_PASSWORD")
                }}
              }}
            }}

            // ----------------------------------------------------------
            // OpenTelemetry trace collection
            otelcol.receiver.otlp "kubernetes_traces" {{
              grpc {{
                endpoint = "0.0.0.0:4317"
              }}

              http {{
                endpoint = "0.0.0.0:4318"
              }}

              output {{
                traces = [otelcol.processor.batch.kubernetes_traces.input]
              }}
            }}

            otelcol.processor.batch "kubernetes_traces" {{
              output {{
                traces = [otelcol.exporter.otlp.grafana_cloud_traces.input]
              }}
            }}

            otelcol.exporter.otlp "grafana_cloud_traces" {{
              client {{
                endpoint = env("GRAFANA_CLOUD_TEMPO_URL")
                auth = otelcol.auth.basic.grafana_cloud_traces.handler
              }}
            }}

            otelcol.auth.basic "grafana_cloud_traces" {{
              username = env("GRAFANA_CLOUD_TEMPO_USERNAME")
              password = env("GRAFANA_CLOUD_TEMPO_PASSWORD")
            }}
            """  # noqa: S608
        )
    },
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        depends_on=[alloy_env_vars_static_secret],
        delete_before_replace=True,
    ),
)

alloy_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-grafana-alloy-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="grafana-alloy",
        chart="alloy",
        version="",
        namespace="operations",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://grafana.github.io/helm-charts",
        ),
        cleanup_on_fail=True,
        skip_await=True,
        values={
            "alloy": {
                "configMap": {
                    "create": False,
                    "name": alloy_configmap_name,
                    "key": "config.alloy",
                },
                "clustering": {
                    "enabled": True,
                },
                "envFrom": [
                    {
                        "secretRef": {
                            "name": alloy_env_vars_secret_name,
                        },
                    },
                ],
                "extraPorts": [
                    {
                        "name": "otlp",
                        "port": 4317,
                        "targetPort": 4317,
                        "appProtocol": "grpc",
                    },
                    {
                        "name": "otlp-http",
                        "port": 4318,
                        "targetPort": 4318,
                    },
                ],
            },
            "serviceAccount": {
                "create": True,
                "additionalLabels": k8s_global_labels,
            },
            "configReloader": {
                "enabled": True,
                "resources": {
                    "requests": {
                        "memory": "10Mi",
                        "cpu": "1m",
                    },
                    "limits": {
                        "memory": "10Mi",
                        "cpu": "1m",
                    },
                },
            },
            "controller": {
                "type": "daemonset",
                "podLabels": k8s_global_labels,
            },
            "service": {
                "enabled": True,
            },
            "serviceMonitor": {
                "enabled": False,
            },
            "ingress": {
                "enabled": False,
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        depends_on=[alloy_configmap],
        delete_before_replace=True,
    ),
)

ksm_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-kube-state-metrics-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="kube-state-metrics",
        chart="oci://registry-1.docker.io/bitnamicharts/kube-state-metrics",
        version=VERSIONS["KUBE_STATE_METRICS_VERSION"],
        namespace="operations",
        cleanup_on_fail=True,
        skip_await=True,
        values={
            "serviceMonitor": {
                "enabled": True,
            },
            "image": {
                "repository": "bitnamilegacy/kube-state-metrics",
                "tag": "2.16.0-debian-12-r5",
            },
            "metricAllowlist": [
                "kube_pod_container_info",
                "kube_pod_container_status_restarts_total",
            ],
            "namespaces": "jupyter",
            "extraArgs": {
                "metric-allowlist": "kube_pod_container_info,kube_pod_container_status_restarts_total",
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        delete_before_replace=True,
    ),
)

# Setup Karpenter
setup_karpenter(
    cluster_name=cluster_name,
    cluster_stack=cluster_stack,
    kms_stack=kms_stack,
    aws_config=aws_config,
    k8s_provider=k8s_provider,
    aws_account=aws_account,
    k8s_global_labels=k8s_global_labels,
)

keda_security_group = aws.ec2.SecurityGroup(
    f"{cluster_name}-keda-security-group",
    description="Security group for KEDA operator",
    vpc_id=target_vpc["id"],
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=DEFAULT_KEDA_PORT,
            to_port=DEFAULT_KEDA_PORT,
            protocol="tcp",
        ),
        aws.ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            protocol="tcp",
        ),
        aws.ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=8080,
            to_port=8080,
            protocol="tcp",
        ),
        aws.ec2.SecurityGroupIngressArgs(
            from_port=6443,
            to_port=6443,
            security_groups=[cluster_stack.require_output("cluster_security_group_id")],
            protocol="tcp",
        ),
    ],
    egress=default_psg_egress_args,
    tags={
        **aws_config.tags,
        "Name": f"{cluster_name}-keda-security-group",
    },
)
export("cluster_keda_security_group_id", keda_security_group.id)

keda_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-keda-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="keda",
        chart="keda",
        version=KEDA_CHART_VERSION,
        namespace="operations",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://kedacore.github.io/charts"
        ),
        cleanup_on_fail=True,
        skip_await=True,
        values={
            "podLabels": {
                "keda": {
                    "ol.mit.edu/pod-security-group": keda_security_group.id,
                },
                "metricsAdapter": {
                    "ol.mit.edu/pod-security-group": keda_security_group.id,
                },
                "webhooks": {
                    "ol.mit.edu/pod-security-group": keda_security_group.id,
                },
            },
            "resources": {
                "operator": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "200Mi",
                    },
                    "limits": {
                        "cpu": "200m",
                        "memory": "400Mi",
                    },
                },
                "metricServer": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "100Mi",
                    },
                    "limits": {
                        "cpu": "200m",
                        "memory": "200Mi",
                    },
                },
                "webhooks": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "100Mi",
                    },
                    "limits": {
                        "cpu": "200m",
                        "memory": "200Mi",
                    },
                },
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        delete_before_replace=True,
    ),
)

keda_security_group_policy = kubernetes.apiextensions.CustomResource(
    f"{cluster_name}-keda-helm-release",
    api_version="vpcresources.k8s.aws/v1beta1",
    kind="SecurityGroupPolicy",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="keda-operator",
        namespace="operations",
        labels=k8s_global_labels,
    ),
    spec={
        "podSelector": {
            "matchLabels": {
                "ol.mit.edu/pod-security-group": keda_security_group.id,
            }
        },
        "securityGroups": {
            "groupIds": [keda_security_group.id],
        },
    },
    opts=ResourceOptions(depends_on=keda_release, provider=k8s_provider),
)

nvidia_k8s_device_plugin_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-nvidia-k8s-device-plugin-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="nvidia-device-plugin",
        chart="nvidia-device-plugin",
        version="0.17.3",
        namespace="operations",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://nvidia.github.io/k8s-device-plugin"
        ),
        cleanup_on_fail=True,
        skip_await=True,
        values={
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "ol.mit.edu/gpu_node",
                                        "operator": "In",
                                        "values": ["true"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
            "tolerations": [
                {
                    "key": "ol.mit.edu/gpu_node",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
            "gfd": {
                "enabled": False,
            },
            "resources": {
                "requests": {
                    "cpu": "100m",
                    "memory": "100Mi",
                },
                "limits": {
                    "cpu": "200m",
                    "memory": "200Mi",
                },
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        delete_before_replace=True,
    ),
)
