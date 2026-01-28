# Create the resources needed to run an edxnotes server

import json
import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity

from bridge.secrets.sops import read_yaml_secrets
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
    Application,
    AWSBase,
    K8sGlobalLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
notes_config = Config("edxnotes")
if Config("vault").get("address"):
    setup_vault_provider()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
edxapp_stack = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

cluster_name = notes_config.get("cluster") or "applications"
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{cluster_name}.{stack_info.name}"
)
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc_name = notes_config.get("target_vpc")
openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)
notes_server_tag = f"edx-notes-server-{env_name}"
target_vpc = network_stack.require_output(target_vpc_name)

dns_zone = dns_stack.require_output(notes_config.require("dns_zone"))
dns_zone_id = dns_zone["id"]

secrets = read_yaml_secrets(Path(f"edx_notes/{env_name}.yaml"))

aws_account = get_caller_identity()

aws_config = AWSBase(
    tags={
        "OU": notes_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-notes",
        "Owner": "platform-engineering",
        "openedx_release": openedx_release,
    }
)

vault.generic.Secret(
    f"edx-notes-{env_name}-configuration-secrets",
    path=f"secret-{stack_info.env_prefix}/edx-notes",
    data_json=json.dumps(secrets),
)

k8s_global_labels = K8sGlobalLabels(
    application=Application.edx_notes,
    product=Product.mitlearn,
    service=Services.edx_notes,
    ou=notes_config.require("business_unit"),
    source_repository="https://github.com/openedx/edx-notes-api",
    stack=stack_info,
)

# Deploy to Kubernetes using OLApplicationK8s component
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Configure namespace
namespace = notes_config.get("namespace") or f"{stack_info.env_prefix}-openedx"

# Determine docker image tag - use digest from environment if available (set by CI),
# otherwise fall back to the openedx release tag
docker_image_tag = os.environ.get("EDX_NOTES_DOCKER_DIGEST", openedx_release)

# Get the VPC that the EKS cluster uses (which has k8s subnets configured)
# When deploying to K8s, use the target VPC (which may be residential, xpro, etc.)
# not the default applications VPC
cluster_vpc = network_stack.require_output(target_vpc_name)
cluster_vpc_id = cluster_vpc["id"]
k8s_pod_subnet_cidrs = cluster_vpc["k8s_pod_subnet_cidrs"]

# Create security group for edx-notes application pods
notes_app_security_group = ec2.SecurityGroup(
    f"edx-notes-app-sg-{env_name}",
    name=f"edx-notes-app-sg-{env_name}",
    description="Security group for edx-notes application pods",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    vpc_id=cluster_vpc_id,
    tags=aws_config.merged_tags({"Name": f"edx-notes-app-{env_name}"}),
)

# Get service URLs from stack references (as Output objects)
opensearch_cluster = opensearch_stack.require_output("cluster")
opensearch_endpoint = opensearch_cluster["endpoint"]
edxapp_output = edxapp_stack.require_output("edxapp")
edxapp_db_address = edxapp_output["mariadb"]

# We use OLApplicationK8sConfig below, which assumes an NGINX sidecar in front
# of the actual application, which we do not have in this case. So we're going
# to define an APP_PORT and use that for all probes as well as the listener port
# of the application (refer to the Dockerfile for usage of APP_PORT)
APP_PORT = 8071

# Application configuration (non-sensitive, static values only)
application_config = {
    "APP_PORT": str(APP_PORT),
    "ELASTICSEARCH_DSL_PORT": "443",
    "ELASTICSEARCH_DSL_USE_SSL": "true",
    "ELASTICSEARCH_DSL_VERIFY_CERTS": "false",
    "DB_NAME": "edx_notes_api",
    "DB_PORT": "3306",
    "DJANGO_SETTINGS_MODULE": "notesserver.settings.env_config",
}

# Read Vault policy template and replace DEPLOYMENT placeholder
vault_policy_template = (
    Path(__file__).parent.joinpath("edx_notes_policy.hcl").read_text()
)
vault_policy_text = vault_policy_template.replace("DEPLOYMENT", stack_info.env_prefix)

# Setup Vault Kubernetes auth using OLEKSAuthBinding
# EDX Notes doesn't need AWS service access (no S3, SES, etc.)
notes_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name=f"edx-notes-{stack_info.env_prefix}",
        namespace=namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_text=vault_policy_text,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="edx-notes",
        vault_sync_service_account_names=f"edx-notes-{stack_info.env_prefix}-vault",
        k8s_labels=k8s_global_labels,
    )
)

vault_k8s_resources = notes_app.vault_k8s_resources

# Create VaultStaticSecret for application secrets and environment config
static_secret_name = "edx-notes-secrets"  # noqa: S105  # pragma: allowlist secret
notes_static_secret = Output.all(
    db_host=edxapp_db_address,
    opensearch_host=opensearch_endpoint,
).apply(
    lambda kwargs: OLVaultK8SSecret(
        f"edx-notes-{env_name}-static-secret",
        OLVaultK8SStaticSecretConfig(
            name="edx-notes-static-secrets",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels.model_dump(),
            dest_secret_name=static_secret_name,
            labels=k8s_global_labels.model_dump(),
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="edx-notes",
            templates={
                "DJANGO_SECRET_KEY": '{{ get .Secrets "django_secret_key" }}',
                "OAUTH_CLIENT_ID": '{{ get .Secrets "oauth_client_id" }}',
                "OAUTH_CLIENT_SECRET": '{{ get .Secrets "oauth_client_secret" }}',
                "DB_HOST": kwargs["db_host"],
                "ELASTICSEARCH_DSL_HOST": kwargs["opensearch_host"],
            },
            refresh_after="1h",
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=vault_k8s_resources,
        ),
    )
)

# Create VaultDynamicSecret for database credentials
db_creds_secret_name = "edx-notes-db-creds"  # noqa: S105  # pragma: allowlist secret
db_creds_secret = OLVaultK8SSecret(
    f"edx-notes-{env_name}-db-creds-secret",
    OLVaultK8SDynamicSecretConfig(
        name="edx-notes-db-creds",
        namespace=namespace,
        dest_secret_labels=k8s_global_labels.model_dump(),
        dest_secret_name=db_creds_secret_name,
        labels=k8s_global_labels.model_dump(),
        mount=f"mariadb-{stack_info.env_prefix}",
        path="creds/notes",
        restart_target_kind="Deployment",
        restart_target_name="edx-notes-app",
        templates={
            "DB_USER": "{{ .Secrets.username }}",
            "DB_PASSWORD": "{{ .Secrets.password }}",
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=vault_k8s_resources,
    ),
)

# Pre-deploy commands for migrations and Elasticsearch index
pre_deploy_commands = [
    ("migrate", ["python", "manage.py", "migrate", "--noinput"]),
    ("es-index", ["python", "manage.py", "search_index", "--rebuild", "-f"]),
]

# Build OLApplicationK8s config
ol_app_k8s_config = OLApplicationK8sConfig(
    project_root=Path(__file__).parent,
    application_config=application_config,
    application_name="edx-notes",
    application_namespace=namespace,
    application_lb_service_name="edx-notes",
    application_lb_service_port_name="http",
    application_min_replicas=notes_config.get_int("min_replicas") or 1,
    application_max_replicas=notes_config.get_int("max_replicas") or 3,
    application_deployment_use_anti_affinity=True,
    k8s_global_labels=k8s_global_labels.model_dump(),
    env_from_secret_names=["edx-notes-secrets", db_creds_secret_name],
    application_security_group_id=notes_app_security_group.id,
    application_security_group_name=notes_app_security_group.name,
    application_service_account_name=None,
    application_image_repository="mitodl/openedx-notes",
    application_image_digest=docker_image_tag,
    application_cmd_array=None,
    application_arg_array=None,
    application_port=APP_PORT,
    vault_k8s_resource_auth_name=f"edx-notes-{stack_info.env_prefix}",
    registry="dockerhub",
    image_pull_policy="IfNotPresent",
    import_nginx_config=False,  # EDX Notes doesn't use nginx/uwsgi pattern
    init_migrations=False,  # We're using pre-deploy jobs instead
    init_collectstatic=False,  # EDX Notes doesn't need collectstatic
    resource_requests={
        "cpu": "250m",
        "memory": "512Mi",
    },
    resource_limits={
        "memory": "512Mi",
    },
    pre_deploy_commands=pre_deploy_commands,
    probe_configs={
        "liveness_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/heartbeat",
                port=APP_PORT,
            ),
            initial_delay_seconds=30,
            period_seconds=10,
        ),
        "readiness_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/heartbeat",
                port=APP_PORT,
            ),
            initial_delay_seconds=10,
            period_seconds=5,
        ),
        "startup_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/heartbeat",
                port=APP_PORT,
            ),
            initial_delay_seconds=10,
            period_seconds=10,
            failure_threshold=6,
        ),
    },
)

# Create the OLApplicationK8s component
edx_notes_k8s_app = OLApplicationK8s(
    ol_app_k8s_config=ol_app_k8s_config,
    opts=ResourceOptions(depends_on=[notes_static_secret, db_creds_secret]),
)

# Gateway API routing + TLS certificate with cert-manager
dns_name = notes_config.get("domain")

gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="edx-notes",
    namespace=namespace,
    labels=k8s_global_labels.model_dump(),
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https-web",
            hostname=dns_name,
            port=8443,
            protocol="HTTPS",
            tls_mode="Terminate",
            certificate_secret_name="edx-notes-tls",  # pragma: allowlist secret  # noqa: E501, S106
            certificate_secret_namespace=namespace,
        ),
    ],
    routes=[
        OLEKSGatewayRouteConfig(
            backend_service_name="edx-notes",
            backend_service_namespace=namespace,
            backend_service_port=APP_PORT,
            name="edx-notes-https-root",
            listener_name="https-web",
            hostnames=[dns_name],
            port=8443,
            matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
        ),
    ],
)

notes_gateway = OLEKSGateway(
    "edx-notes-gateway",
    gateway_config=gateway_config,
)

# Export Kubernetes resources
export("k8s_deployment_name", "edx-notes-app")
export("k8s_service_name", "edx-notes")
export("k8s_namespace", namespace)
export("notes_domain", dns_name)
export("security_group_id", notes_app_security_group.id)
