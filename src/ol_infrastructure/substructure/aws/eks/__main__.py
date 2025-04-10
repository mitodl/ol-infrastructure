# ruff: noqa: E501

import json
import os
import re
import textwrap
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import AWS_EVENT_TARGET_GROUP_NAME_MAX_LENGTH
from bridge.lib.versions import VANTAGE_K8S_AGENT_CHART_VERSION
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.substructure.aws.eks.karpenter_iam import (
    get_cluster_karpenter_iam_policy_document,
)

env_config = Config("environment")

aws_account = aws.get_caller_identity()

VERSIONS = {
    "VANTAGE_K8S_AGENT_VERSION": os.environ.get(
        "VANTAGE_K8S_AGENT_CHART_VERSION", VANTAGE_K8S_AGENT_CHART_VERSION
    )
}

stack_info = parse_stack()

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)
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
            logging {{
              level = "info"
              format = "logfmt"
            }}

            // Collect all pod logs
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


############################################################
# Install Karpenter for automatically growing and shrinking
# the cluster
############################################################
# Karpenter Interruption Queue
karpenter_interruption_queue = aws.sqs.Queue(
    f"{cluster_name}-karpenter-interruption-queue",
    name=cluster_name,
    message_retention_seconds=300,
    sqs_managed_sse_enabled=True,
    tags=aws_config.merged_tags({"Name": cluster_name}),
)

karpenter_interruption_queue_policy = aws.sqs.QueuePolicy(
    f"{cluster_name}-karpenter-interruption-queue-policy",
    queue_url=karpenter_interruption_queue.id,
    policy=Output.all(
        queue_arn=karpenter_interruption_queue.arn, partition=aws.get_partition()
    ).apply(
        lambda args: json.dumps(
            {
                "Version": "2012-10-17",
                "Id": "EC2InterruptionPolicy",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": ["events.amazonaws.com", "sqs.amazonaws.com"]
                        },
                        "Action": "sqs:SendMessage",
                        "Resource": args["queue_arn"],
                    },
                    {
                        "Sid": "DenyHTTP",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "sqs:*",
                        "Resource": args["queue_arn"],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    },
                ],
            }
        )
    ),
)

# EventBridge Rules targeting the interruption queue
event_patterns = {
    "scheduled-change": {
        "source": ["aws.health"],
        "detail-type": ["AWS Health Event"],
    },
    "spot-interruption": {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Spot Instance Interruption Warning"],
    },
    "rebalance": {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Instance Rebalance Recommendation"],
    },
    "instance-state-change": {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Instance State-change Notification"],
    },
}

for rule_name_suffix, event_pattern in event_patterns.items():
    rule = aws.cloudwatch.EventRule(
        f"{cluster_name}-karpenter-interruption-{rule_name_suffix}-rule",
        name=f"{cluster_name}-karpenter-{rule_name_suffix}"[
            :AWS_EVENT_TARGET_GROUP_NAME_MAX_LENGTH
        ],
        event_pattern=json.dumps(event_pattern),
        tags=aws_config.tags,
    )
    aws.cloudwatch.EventTarget(
        f"{cluster_name}-karpenter-interruption-{rule_name_suffix}-target",
        target_id=f"{cluster_name}-karpenter-interruption-{rule_name_suffix}"[
            :AWS_EVENT_TARGET_GROUP_NAME_MAX_LENGTH
        ],
        rule=rule.name,
        arn=karpenter_interruption_queue.arn,
    )


# Karpenter Controller Trust Role (IAM Role for Service Account - IRSA)
karpenter_trust_role = OLEKSTrustRole(
    f"{cluster_name}-karpenter-controller-trust-role",
    role_config=OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        description="Trust role for allowing karpenter to create and destroy "
        "ec2 instances from within the cluster.",
        policy_operator="StringEquals",
        role_name="karpenter",
        service_account_identifier="system:serviceaccount:operations:karpenter",  # Matches the Helm chart default SA name
        tags=aws_config.tags,
    ),
    opts=ResourceOptions(),
)

# Generate and create the Karpenter Controller IAM Policy
karpenter_controller_policy_document = Output.all(
    partition=aws.get_partition().partition,
    region=aws.get_region().name,
    account_id=aws_account.account_id,
    cluster_name=cluster_name,
    interruption_queue_arn=karpenter_interruption_queue.arn,
    node_role_arn=cluster_stack.require_output("node_role_arn"),
).apply(
    lambda args: get_cluster_karpenter_iam_policy_document(
        aws_partition=args["partition"],
        aws_region=args["region"],
        aws_account_id=args["account_id"],
        cluster_name=args["cluster_name"],
        karpenter_interruption_queue_arn=args["interruption_queue_arn"],
        karpenter_node_role_arn=args["node_role_arn"],
    )
)

karpenter_controller_policy = aws.iam.Policy(
    f"{cluster_name}-karpenter-controller-policy",
    name=f"KarpenterControllerPolicy-{cluster_name}",
    policy=karpenter_controller_policy_document.apply(json.dumps),
    tags=aws_config.tags,
)

# Attach the Controller Policy to the Trust Role
aws.iam.RolePolicyAttachment(
    f"{cluster_name}-karpenter-controller-policy-attachment",
    role=karpenter_trust_role.role.name,
    policy_arn=karpenter_controller_policy.arn,
)


# Install Karpenter Helm Chart
karpenter_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-karpenter-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="karpenter",
        chart="oci://public.ecr.aws/karpenter/karpenter",
        version="1.3.2",  # Specify a version for stability
        namespace="operations",  # Deploy into the operations namespace
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://charts.karpenter.sh",
        ),
        cleanup_on_fail=True,
        skip_await=False,  # Wait for resources to be ready
        values={
            # Configure IRSA
            "serviceAccount": {
                "create": True,  # Let the chart create the SA
                "name": "karpenter",
                "annotations": {
                    "eks.amazonaws.com/role-arn": karpenter_trust_role.role.arn,
                },
            },
            "controller": {
                "resources": {
                    "requests": {
                        "cpu": "100m",
                        "memory": "256Mi",
                    },
                    "limits": {
                        "cpu": "200m",
                        "memory": "512Mi",
                    },
                },
            },
            "settings": {
                # Use cluster name and endpoint from the EKS stack output
                "clusterName": cluster_name,
                "clusterEndpoint": cluster_stack.require_output("kube_config_data")[
                    "server"
                ],
                # Configure interruption handling
                "interruptionQueue": karpenter_interruption_queue.name,
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[
            karpenter_trust_role,
            karpenter_controller_policy,  # Ensure policy exists before Helm install
            karpenter_interruption_queue,  # Ensure queue exists
        ],
        delete_before_replace=True,  # Useful for Helm upgrades/changes
    ),
)

# --- Dynamically determine the EKS Optimized AL2023 AMI Alias ---
# Get cluster version from the referenced stack
cluster_version = cluster_stack.require_output("cluster_version")

# Construct the SSM parameter name dynamically
ssm_parameter_name = cluster_version.apply(
    lambda version: f"/aws/service/eks/optimized-ami/{version}/amazon-linux-2023/x86_64/standard/recommended/image_id"
)

# Get the recommended AMI ID from SSM Parameter Store as an Output
recommended_ami_id_output = aws.ssm.get_parameter_output(
    name=ssm_parameter_name,
).apply(lambda param_result: param_result.value)  # Apply to get Output[str]

# Get the AMI details using the recommended AMI ID (which is an Output[str])
# We need to call get_ami within the apply block
recommended_ami_output = recommended_ami_id_output.apply(
    lambda ami_id: aws.ec2.get_ami(  # Call get_ami inside apply
        filters=[
            aws.ec2.GetAmiFilterArgs(
                name="image-id",
                values=[ami_id],  # Use the resolved ami_id here
            ),
        ],
        owners=["amazon"],  # EKS optimized AMIs are owned by Amazon
        most_recent=True,
    )
)


# Extract the version string from the AMI name (e.g., "amazon-eks-node-al2023-x86_64-standard-1.29-v20240328")
def extract_version(ami_name: str) -> str:
    """Extract the version suffix (e.g., vYYYYMMDD) from an AMI name."""
    import pulumi  # Import needed for pulumi.log within apply

    match = re.search(r"(v\d+)$", ami_name)
    if match:
        return match.group(1)
    else:
        # Fallback or error handling if pattern doesn't match
        # Using 'latest' might cause unintended upgrades; raising an error might be safer in prod.
        # For now, log a warning and use latest as per Karpenter docs example.
        pulumi.log.warn(
            f"Could not extract version from AMI name: {ami_name}. Defaulting to 'latest'."
        )
        return "latest"


ami_version_string = recommended_ami_output.apply(lambda ami: extract_version(ami.name))

# Construct the alias using the extracted version
ami_alias = ami_version_string.apply(lambda version: f"al2023@{version}")
# --- End AMI Alias Determination ---


default_node_class = kubernetes.apiextensions.CustomResource(
    f"{cluster_name}-karpenter-default-node-class",
    api_version="karpenter.k8s.aws/v1",  # Correct API version for EC2NodeClass
    kind="EC2NodeClass",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="default",
        namespace="operations",
        labels=k8s_global_labels,
    ),
    spec={
        "kubelet": {},
        "subnetSelectorTerms": cluster_stack.require_output("pod_subnet_ids").apply(
            lambda ids: [{"id": subnet_id} for subnet_id in ids]
        ),
        "securityGroupSelectorTerms": [
            {"id": cluster_stack.require_output("node_security_group_id")},
            {"id": cluster_stack.require_output("node_group_security_group_id")},
        ],
        "instanceProfile": cluster_stack.require_output("node_instance_profile"),
        # Dynamically select the EKS Optimized AL2023 AMI based on cluster version
        "amiSelectorTerms": [
            {"alias": ami_alias},
        ],
        "tags": aws_config.merged_tags(
            {"Name": f"{cluster_name}-karpenter-default-nodeclass"}
        ),
    },
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[karpenter_release],  # Ensure Karpenter CRDs are available
    ),
)

default_node_pool = kubernetes.apiextensions.CustomResource(
    f"{cluster_name}-karpenter-default-node-pool",
    api_version="karpenter.sh/v1",
    kind="NodePool",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="default",
        namespace="operations",
        labels=k8s_global_labels,
    ),
    spec={
        "template": {
            "metadata": {
                "labels": k8s_global_labels,
            },
            "spec": {
                "nodeClassRef": {
                    "group": "karpenter.k8s.aws",
                    "kind": "EC2NodeClass",
                    "name": "default",
                },
                "expireAfter": "720h",
                "terminationGracePeriod": "48h",
                "requirements": [
                    {
                        "key": "karpenter.k8s.aws/instance-category",
                        "operator": "In",
                        "values": ["m"],
                    },
                    {
                        "key": "karpenter.k8s.aws/instance-family",
                        "operator": "In",
                        "values": ["m7a", "m7"],
                    },
                    {
                        "key": "kubernetes.io/arch",
                        "operator": "In",
                        "values": ["amd64"],
                    },
                    {
                        "key": "kubernetes.io/os",
                        "operator": "In",
                        "values": ["linux"],
                    },
                    {
                        "key": "karpenter.sh/capacity-type",
                        "operator": "In",
                        "values": ["on-demand"],
                    },
                ],
            },
        },
        "disruption": {
            "consolidationPolicy": "WhenEmptyOrUnderutilized",
            "consolidateAfter": "2m",
        },
        "limits": {
            "cpu": "64",
            "memory": "128Gi",
        },
    },
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[karpenter_release],
    ),
)
