"""Create the resources needed to run an xqueue server."""

import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity

from bridge.lib.magic_numbers import XQUEUE_SERVICE_PORT
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
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
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
xqueue_config = Config("xqueue")
if Config("vault").get("address"):
    setup_vault_provider()

# Load shared infrastructure stacks
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
edxapp_stack = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

cluster_name = xqueue_config.get("cluster") or "applications"
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{cluster_name}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc_name = xqueue_config.get("target_vpc")

openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)

aws_account = get_caller_identity()

aws_config = AWSBase(
    tags={
        "OU": xqueue_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-xqueue",
        "Owner": "platform-engineering",
    }
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.xqueue,
    ou=xqueue_config.require("business_unit"),
    stack=stack_info,
)

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Configure namespace
namespace = xqueue_config.get("namespace") or f"{stack_info.env_prefix}-openedx"

# Determine docker image tag
# Use explicit SHA digest if provided, otherwise use release tag
docker_image_tag = (
    os.environ.get("XQUEUE_DOCKER_DIGEST")
    or xqueue_config.get("docker_tag")
    or openedx_release
)

# Get the VPC that the EKS cluster uses
cluster_vpc = network_stack.require_output(target_vpc_name)
cluster_vpc_id = cluster_vpc["id"]
k8s_pod_subnet_cidrs = cluster_vpc["k8s_pod_subnet_cidrs"]

# Create security group for xqueue application pods
xqueue_app_security_group = ec2.SecurityGroup(
    f"xqueue-app-sg-{env_name}",
    name=f"xqueue-app-sg-{env_name}",
    description="Security group for xqueue application pods",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    vpc_id=cluster_vpc_id,
    tags=aws_config.merged_tags({"Name": f"xqueue-app-{env_name}"}),
)

# Application configuration using environment variables
# The new env_config.py Django settings module reads from environment variables
# instead of XQUEUE_CFG pointing to a YAML file
# Note: DB_HOST is Output, so we set it separately via deployment patch
application_config = {
    "DB_NAME": "xqueue",
    "DB_PORT": "3306",
    "CONSUMER_DELAY": "10",
    "SUBMISSION_PROCESSING_DELAY": "1",
    "LOCAL_LOGLEVEL": "INFO",
    "LOGGING_ENV": "prod",
    "LOG_DIR": "/edx/var/logs/xqueue",
    "NEWRELIC_LICENSE_KEY": "not-a-valid-key",
    "CSRF_COOKIE_SECURE": "false",
    "SESSION_COOKIE_SECURE": "false",
    "SYSLOG_SERVER": "localhost",
    "UPLOAD_PATH_PREFIX": "xqueue",
    "XQUEUE_CFG": "/dev/null",
}

db_host = edxapp_stack.require_output("edxapp")["mariadb"]

# Read Vault policy template and replace DEPLOYMENT placeholder
vault_policy_template = Path(__file__).parent.joinpath("xqueue_policy.hcl").read_text()
vault_policy_text = vault_policy_template.replace("DEPLOYMENT", stack_info.env_prefix)

# Setup Vault Kubernetes auth using OLEKSAuthBinding
xqueue_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name=f"xqueue-{stack_info.env_prefix}",
        namespace=namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_text=vault_policy_text,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="xqueue",
        vault_sync_service_account_names=f"xqueue-{stack_info.env_prefix}-vault",
        k8s_labels=k8s_global_labels,
    )
)

vault_k8s_resources = xqueue_app.vault_k8s_resources

# Create VaultSecret for database credentials
# These are exposed as environment variables to the Django settings module
db_creds_secret_name = "xqueue-db-creds"  # noqa: S105  # pragma: allowlist secret


def create_db_creds_secret(db_address: str) -> OLVaultK8SSecret:
    """Create database credentials secret with resolved DB_HOST."""
    return OLVaultK8SSecret(
        f"xqueue-{env_name}-db-creds-secret",
        OLVaultK8SDynamicSecretConfig(
            name="xqueue-db-creds",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels.model_dump(),
            dest_secret_name=db_creds_secret_name,
            labels=k8s_global_labels.model_dump(),
            mount=f"mariadb-{stack_info.env_prefix}",
            path="creds/xqueue",
            restart_target_kind="Deployment",
            restart_target_name="xqueue-app",
            templates={
                "DB_USER": "{{ .Secrets.username }}",
                "DB_PASSWORD": "{{ .Secrets.password }}",
                "DB_HOST": db_address,
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=vault_k8s_resources,
        ),
    )


db_creds_secret = db_host.apply(create_db_creds_secret)

# Create VaultSecret for user credentials (edxapp and xqwatcher passwords)
xqueue_creds_secret_name = "xqueue-user-creds"  # noqa: S105  # pragma: allowlist secret
xqueue_creds_secret = OLVaultK8SSecret(
    f"xqueue-{env_name}-user-creds-secret",
    OLVaultK8SStaticSecretConfig(
        name="xqueue-user-creds",
        namespace=namespace,
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name=xqueue_creds_secret_name,
        labels=k8s_global_labels.model_dump(),
        mount=f"secret-{stack_info.env_prefix}",
        mount_type="kv-v1",
        path="edx-xqueue",
        templates={
            "XQUEUE_EDXAPP_PASSWORD": "{{ .Secrets.edxapp_password }}",
            "XQUEUE_XQWATCHER_PASSWORD": "{{ .Secrets.xqwatcher_password }}",
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=vault_k8s_resources,
    ),
)

# Build OLApplicationK8s config
ol_app_k8s_config = OLApplicationK8sConfig(
    project_root=Path(__file__).parent,
    application_config=application_config,
    application_name="xqueue",
    application_namespace=namespace,
    application_lb_service_name="xqueue",
    application_lb_service_port_name="http",
    application_min_replicas=xqueue_config.get_int("min_replicas") or 1,
    application_max_replicas=xqueue_config.get_int("max_replicas") or 3,
    application_deployment_use_anti_affinity=True,
    k8s_global_labels=k8s_global_labels.model_dump(),
    env_from_secret_names=[db_creds_secret_name, xqueue_creds_secret_name],
    application_security_group_id=xqueue_app_security_group.id,
    application_security_group_name=xqueue_app_security_group.name,
    application_service_account_name=None,
    application_image_repository="mitodl/xqueue",
    application_docker_tag=docker_image_tag,
    application_cmd_array=None,
    application_arg_array=None,
    vault_k8s_resource_auth_name=f"xqueue-{stack_info.env_prefix}",
    registry="dockerhub",
    image_pull_policy="IfNotPresent",
    import_nginx_config=False,
    init_migrations=False,
    init_collectstatic=False,
    application_port=XQUEUE_SERVICE_PORT,  # xqueue listens directly on 8040
    resource_requests={
        "cpu": "500m",
        "memory": "512Mi",
    },
    resource_limits={
        "memory": "512Mi",
    },
    probe_configs={
        "liveness_probe": kubernetes.core.v1.ProbeArgs(
            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                port=XQUEUE_SERVICE_PORT,
            ),
            initial_delay_seconds=30,
            period_seconds=10,
        ),
        "readiness_probe": kubernetes.core.v1.ProbeArgs(
            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                port=XQUEUE_SERVICE_PORT,
            ),
            initial_delay_seconds=10,
            period_seconds=5,
        ),
        "startup_probe": kubernetes.core.v1.ProbeArgs(
            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                port=XQUEUE_SERVICE_PORT,
            ),
            initial_delay_seconds=10,
            period_seconds=10,
            failure_threshold=6,
        ),
    },
)

# Create the OLApplicationK8s component
# db_creds_secret is an Output, so we need to unwrap it for depends_on
xqueue_k8s_app = OLApplicationK8s(
    ol_app_k8s_config=ol_app_k8s_config,
    opts=ResourceOptions(depends_on=[db_creds_secret, xqueue_creds_secret]),
)

# Gateway API routing with TLS certificate for external HTTPS access
# External clients connect via HTTPS on port 8443
# Internal pods connect via HTTP on port 8040
dns_name = xqueue_config.get("domain")

gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="xqueue",
    namespace=namespace,
    labels=k8s_global_labels.model_dump(),
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https-web",
            hostname=dns_name,
            port=8443,
            protocol="HTTPS",
            tls_mode="Terminate",
            certificate_secret_name="xqueue-tls",  # pragma: allowlist secret  # noqa: E501, S106
            certificate_secret_namespace=namespace,
        ),
    ],
    routes=[
        OLEKSGatewayRouteConfig(
            backend_service_name="xqueue",
            backend_service_namespace=namespace,
            backend_service_port=XQUEUE_SERVICE_PORT,  # Internal port 8040
            name="xqueue-https-root",
            listener_name="https-web",
            hostnames=[dns_name],
            port=8443,
            matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
        ),
    ],
)

xqueue_gateway = OLEKSGateway(
    "xqueue-gateway",
    gateway_config=gateway_config,
)

# Export Kubernetes resources
export("k8s_deployment_name", "xqueue-app")
export("k8s_service_name", "xqueue")
export("k8s_namespace", namespace)
export("xqueue_domain", dns_name)
export("security_group_id", xqueue_app_security_group.id)
export("django_settings_module", "xqueue.env_config")
export("db_credentials_secret", db_creds_secret_name)
export("user_credentials_secret", xqueue_creds_secret_name)
