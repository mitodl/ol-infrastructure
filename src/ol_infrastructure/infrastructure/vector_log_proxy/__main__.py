"""Create the resources needed to run a vector-log-proxy server in Kubernetes."""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import get_caller_identity, s3

from bridge.lib.versions import VECTOR_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.ol_types import AWSBase, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

stack_info = parse_stack()
vector_log_proxy_config = Config("vector_log_proxy")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
namespace = "operations"
application_name = "vector-log-proxy"

# Ports for the proxy services (internal)
HEROKU_LOG_PROXY_PORT = 9000
FASTLY_LOG_PROXY_PORT = 9443

aws_config = AWSBase(
    tags={
        "OU": vector_log_proxy_config.get("business_unit") or "operations",
        "Environment": f"{env_name}",
    }
)

aws_account = get_caller_identity()

k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
    "ol.mit.edu/stack": stack_info.full_name,
}

k8s_labels = K8sGlobalLabels(
    service=Services.vector_log_proxy,
    ou="operations",
    stack=stack_info,
)

target_vpc_name = (
    vector_log_proxy_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
)
target_vpc = network_stack.require_output(target_vpc_name)
vpc_id = target_vpc["id"]

##################################
#   Kubernetes Provider Setup    #
##################################

k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster_stack.require_output("kube_config"),
)

##################################
#     Vault + Secrets Setup      #
##################################

# Create Vault secrets backend for vector-log-proxy
vector_log_proxy_secrets_mount = vault.Mount(
    "vector-log-proxy-app-secrets",
    description="Generic secrets storage for vector-log-proxy deployment",
    path="secret-vector-log-proxy",
    type="kv-v2",
)

# Read secrets from SOPS-encrypted files
proxy_credentials = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

heroku_proxy_credentials = proxy_credentials["heroku"]
fastly_proxy_credentials = proxy_credentials["fastly"]

# Store proxy and Grafana credentials in Vault
vault.generic.Secret(
    "vector-log-proxy-http-auth-creds",
    path=vector_log_proxy_secrets_mount.path.apply(
        lambda mount_path: f"{mount_path}/basic_auth_credentials"
    ),
    data_json=json.dumps(
        {
            "fastly": {
                "username": fastly_proxy_credentials["username"],
                "password": fastly_proxy_credentials["password"],
            },
            "heroku": {
                "username": heroku_proxy_credentials["username"],
                "password": heroku_proxy_credentials["password"],
            },
            "grafana_api_key": grafana_credentials["api_key"],
            "grafana_prometheus_user_id": grafana_credentials["prometheus_user_id"],
            "grafana_loki_user_id": grafana_credentials["loki_user_id"],
        }
    ),
)

##################################
#  IAM + Vault Auth Binding      #
##################################

# Set up IAM role (IRSA) and Vault auth for Vector pods
# Vector doesn't need S3 access (only writes to Grafana Cloud)
# But we set up the binding for Vault secret access
vector_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="vector-log-proxy",
        namespace=namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,  # No AWS permissions needed
        vault_policy_path=Path(__file__).parent.joinpath("vector_log_proxy_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="vector-log-proxy",
        vault_sync_service_account_names="vector-log-proxy-vault",
        k8s_labels=k8s_labels,
    )
)

# Sync credentials from Vault to Kubernetes secret
vector_log_proxy_credentials_secret = OLVaultK8SSecret(
    f"vector-log-proxy-credentials-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="vector-log-proxy-vault-secret-sync",
        namespace=namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name="vector-log-proxy-credentials",  # noqa: S106  # pragma: allowlist secret
        dest_secret_type="Opaque",  # noqa: S106  # pragma: allowlist secret
        mount="secret-vector-log-proxy",
        mount_type="kv-v2",
        path="basic_auth_credentials",
        templates={
            "fastly_username": "{{ .Secrets.fastly.username }}",
            # pragma: allowlist secret
            "fastly_password": "{{ .Secrets.fastly.password }}",
            "heroku_username": "{{ .Secrets.heroku.username }}",
            # pragma: allowlist secret
            "heroku_password": "{{ .Secrets.heroku.password }}",
            "grafana_api_key": "{{ .Secrets.grafana_api_key }}",
            "grafana_prometheus_user_id": ("{{ .Secrets.grafana_prometheus_user_id }}"),
            "grafana_loki_user_id": "{{ .Secrets.grafana_loki_user_id }}",
        },
        refresh_after="1h",
        vaultauth=vector_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
        depends_on=[vector_auth_binding.vault_k8s_resources.vso_resources],
    ),
)

##################################
#     S3 Fastly Challenge Bucket #
##################################

fastly_service_hash_bucket_name = f"mitodl-vector-log-proxy-static-{env_name}"
fastly_service_hash_bucket = s3.Bucket(
    f"vector-log-proxy-fastly-service-hash-bucket-{env_name}",
    bucket=fastly_service_hash_bucket_name,
    acl="public-read",
    tags=aws_config.tags,
)

service_hash_content = ""
for service_hash in vector_log_proxy_config.get_object(
    "fastly_service_id_sha256sums"
).values():
    service_hash_content = service_hash_content + service_hash + "\n"

fastly_service_hash_bucket_name_with_prefix = ".well-known/fastly/logging/challenge"
fastly_service_hash_object = s3.BucketObjectv2(
    fastly_service_hash_bucket_name_with_prefix,
    bucket=fastly_service_hash_bucket_name,
    acl="public-read",
    content=service_hash_content,
    content_type="text/plain",
    opts=ResourceOptions(parent=fastly_service_hash_bucket),
)

service_hash_bucket_fqdn = fastly_service_hash_bucket.bucket_domain_name.apply(
    lambda domain: f"{domain}"
)
challenge_url = fastly_service_hash_bucket.bucket_domain_name.apply(
    lambda domain: f"https://{domain}/{fastly_service_hash_bucket_name_with_prefix}"
)
export(
    "fastly_service_hash_bucket",
    {"challenge_url": challenge_url, "bucket_fqdn": service_hash_bucket_fqdn},
)

##################################
#     Vector Configuration       #
##################################

# Build Vector configuration
vector_config_template = """
api:
  enabled: false

sources:
  fastly_log_proxy:
    type: http_server
    address: 0.0.0.0:9443
    auth:
      password: ${FASTLY_PROXY_PASSWORD}
      username: ${FASTLY_PROXY_USERNAME}
    decoding:
      codec: bytes

  heroku_log_proxy:
    type: heroku_logs
    acknowledgements: false
    address: 0.0.0.0:9000
    decoding:
      codec: bytes
    auth:
      password: "${HEROKU_PROXY_PASSWORD}"
      username: "${HEROKU_PROXY_USERNAME}"
    query_parameters:
    - "app_name"
    - "environment"
    - "service"

transforms:
  fastly_drop_unwanted_logs:
    type: remap
    inputs:
    - "fastly_log_proxy"
    source: |
      event, err = parse_json(.message)
      if event != null {
        .,err = merge(., event)
        del(.message)
      }

  heroku_drop_unwanted_logs:
    type: remap
    inputs:
    - "heroku_log_proxy"
    source: |
      # Drop all messages from uninteresting heroku apps
      abort_match_boring_apps, err = (match_any(.app_name,
        [r'ol-eng-library', r'.*wiki.*']))
      if abort_match_boring_apps {
        abort
      }

sinks:
  ship_fastly_logs_to_grafana_cloud:
    inputs:
    - 'fastly_drop_unwanted_logs'
    type: loki
    auth:
      strategy: basic
      password: ${GRAFANA_CLOUD_API_KEY}
      user: "${GRAFANA_CLOUD_LOKI_API_USER-loki}"
    endpoint: https://logs-prod-us-central1.grafana.net
    encoding:
      codec: json
    labels:
      environment: "{{ environment }}"
      application: "{{ application }}"
      service: "fastly"
    out_of_order_action: rewrite_timestamp

  ship_heroku_logs_to_grafana_cloud:
    inputs:
    - 'heroku_drop_unwanted_logs'
    type: loki
    auth:
      strategy: basic
      password: ${GRAFANA_CLOUD_API_KEY}
      user: "${GRAFANA_CLOUD_LOKI_API_USER-loki}"
    endpoint: https://logs-prod-us-central1.grafana.net
    encoding:
      codec: json
    labels:
      environment: "{{ environment }}"
      application: "{{ app_name }}"
      service: "{{ service }}"
    out_of_order_action: rewrite_timestamp
"""

vector_config_map = kubernetes.core.v1.ConfigMap(
    "vector-log-proxy-config",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        namespace=namespace,
        name="vector-log-proxy-config",
        labels=k8s_global_labels,
    ),
    data={
        "vector.yaml": vector_config_template,
    },
    opts=ResourceOptions(provider=k8s_provider),
)

##################################
#  Service Account + RBAC        #
##################################

# Service account for Vector pods with IRSA annotation
vector_service_account = kubernetes.core.v1.ServiceAccount(
    "vector-log-proxy-service-account",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        namespace=namespace,
        name="vector-log-proxy",
        labels=k8s_global_labels,
        annotations={
            # pragma: allowlist secret
            "eks.amazonaws.com/role-arn": vector_auth_binding.irsa_role.arn,
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[vector_auth_binding],
    ),
)

##################################
#  Kubernetes Deployment         #
##################################

vector_deployment = kubernetes.apps.v1.Deployment(
    "vector-log-proxy-deployment",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        namespace=namespace,
        name=application_name,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=2,
        strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
            type="RollingUpdate",
            rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                max_surge="25%",
                max_unavailable="25%",
            ),
        ),
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=k8s_global_labels,
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=k8s_global_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name="vector-log-proxy",
                containers=[
                    # Vector log processing
                    kubernetes.core.v1.ContainerArgs(
                        name="vector",
                        image=cached_image_uri(
                            f"timberio/vector:{VECTOR_VERSION}-alpine"
                        ),
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="heroku", container_port=HEROKU_LOG_PROXY_PORT
                            ),
                            kubernetes.core.v1.ContainerPortArgs(
                                name="fastly", container_port=FASTLY_LOG_PROXY_PORT
                            ),
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="ENVIRONMENT",
                                value=stack_info.env_suffix,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="APPLICATION",
                                value="vector-log-proxy",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="SERVICE",
                                value="vector-log-proxy",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="VECTOR_CONFIG_DIR",
                                value="/etc/vector/",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="VECTOR_STRICT_ENV_VARS",
                                value="false",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="AWS_REGION",
                                value="us-east-1",
                            ),
                            # Grafana credentials from Vault secrets
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAFANA_CLOUD_API_KEY",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="grafana_api_key",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAFANA_CLOUD_PROMETHEUS_API_USER",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="grafana_prometheus_user_id",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAFANA_CLOUD_LOKI_API_USER",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="grafana_loki_user_id",
                                    ),
                                ),
                            ),
                            # Proxy credentials from Vault secrets
                            kubernetes.core.v1.EnvVarArgs(
                                name="HEROKU_PROXY_PASSWORD",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="heroku_password",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="HEROKU_PROXY_USERNAME",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="heroku_username",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="FASTLY_PROXY_PASSWORD",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="fastly_password",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="FASTLY_PROXY_USERNAME",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name="vector-log-proxy-credentials",
                                        key="fastly_username",
                                    ),
                                ),
                            ),
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="vector-config",
                                mount_path="/etc/vector",
                            ),
                        ],
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=HEROKU_LOG_PROXY_PORT,
                            ),
                            initial_delay_seconds=15,
                            period_seconds=30,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=HEROKU_LOG_PROXY_PORT,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=10,
                        ),
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "500m",
                                "memory": "512Mi",
                            },
                            limits={
                                "cpu": "1000m",
                                "memory": "1Gi",
                            },
                        ),
                    ),
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="vector-config",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name="vector-log-proxy-config",
                        ),
                    ),
                ],
                affinity=kubernetes.core.v1.AffinityArgs(
                    pod_anti_affinity=kubernetes.core.v1.PodAntiAffinityArgs(
                        preferred_during_scheduling_ignored_during_execution=[
                            kubernetes.core.v1.WeightedPodAffinityTermArgs(
                                weight=100,
                                pod_affinity_term=kubernetes.core.v1.PodAffinityTermArgs(
                                    label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                                        match_labels=k8s_global_labels,
                                    ),
                                    topology_key="kubernetes.io/hostname",
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        ),
    ),
    opts=ResourceOptions(provider=k8s_provider),
)

##################################
#     Service Exposure           #
##################################

vector_service = kubernetes.core.v1.Service(
    "vector-log-proxy-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        namespace=namespace,
        name=application_name,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector=k8s_global_labels,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="heroku",
                port=HEROKU_LOG_PROXY_PORT,
                target_port="heroku",
                protocol="TCP",
            ),
            kubernetes.core.v1.ServicePortArgs(
                name="fastly",
                port=FASTLY_LOG_PROXY_PORT,
                target_port="fastly",
                protocol="TCP",
            ),
        ],
    ),
    opts=ResourceOptions(provider=k8s_provider),
)

##################################
#     Gateway Configuration      #
##################################

vector_domain = vector_log_proxy_config.require("web_host_domain")

vector_gateway = OLEKSGateway(
    f"vector-log-proxy-{stack_info.name}-gateway",
    gateway_config=OLEKSGatewayConfig(
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        gateway_name="vector-log-proxy-gateway",
        labels=k8s_global_labels,
        namespace=namespace,
        http_redirect=True,
        listeners=[
            OLEKSGatewayListenerConfig(
                name="https",
                hostname=vector_domain,
                port=8443,
                protocol="HTTPS",
                tls_mode="Terminate",
                certificate_secret_name="vector-log-proxy-tls",  # noqa: S106  # pragma: allowlist secret
                certificate_secret_namespace=namespace,
            ),
        ],
        routes=[
            # Fastly service hash challenge route (must come first for specificity)
            OLEKSGatewayRouteConfig(
                backend_service_name=None,
                backend_service_namespace=None,
                backend_service_port=None,
                hostnames=[vector_domain],
                name="vector-log-proxy-fastly-challenge",
                listener_name="https",
                port=8443,
                matches=[
                    {
                        "path": {
                            "type": "PathPrefix",
                            "value": "/.well-known/fastly/logging/challenge",
                        }
                    }
                ],
                filters=[
                    {
                        "type": "RequestRedirect",
                        "requestRedirect": {
                            "scheme": "https",
                            "hostname": service_hash_bucket_fqdn,
                            "statusCode": 301,
                        },
                    }
                ],
            ),
            OLEKSGatewayRouteConfig(
                backend_service_name=application_name,
                backend_service_namespace=namespace,
                backend_service_port=HEROKU_LOG_PROXY_PORT,
                hostnames=[vector_domain],
                name="vector-log-proxy-heroku",
                listener_name="https",
                port=8443,
                matches=[{"path": {"type": "PathPrefix", "value": "/heroku"}}],
                filters=[
                    {
                        "type": "URLRewrite",
                        "urlRewrite": {
                            "path": {
                                "type": "ReplacePrefixMatch",
                                "replacePrefixMatch": "/",
                            }
                        },
                    }
                ],
            ),
            OLEKSGatewayRouteConfig(
                backend_service_name=application_name,
                backend_service_namespace=namespace,
                backend_service_port=FASTLY_LOG_PROXY_PORT,
                hostnames=[vector_domain],
                name="vector-log-proxy-fastly",
                listener_name="https",
                port=8443,
                matches=[{"path": {"type": "PathPrefix", "value": "/fastly"}}],
                filters=[
                    {
                        "type": "URLRewrite",
                        "urlRewrite": {
                            "path": {
                                "type": "ReplacePrefixMatch",
                                "replacePrefixMatch": "/",
                            }
                        },
                    }
                ],
            ),
        ],
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)

##################################
#     Exports                    #
##################################

export("vector_log_proxy", {"fqdn": vector_domain})
export("vector_log_proxy_service", vector_service.metadata["name"])
export("vector_log_proxy_namespace", namespace)
export("vector_log_proxy_domain", vector_domain)
