"""Deploy Superset to EKS."""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity, route53, ses

from bridge.lib.magic_numbers import (
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
    FIVE_MINUTES,
)
from bridge.lib.versions import SUPERSET_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
superset_config = Config("superset")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
vault_infra_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)
policy_stack = StackReference("infrastructure.aws.policies")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

mitol_zone_id = dns_stack.require_output("ol")["id"]
operations_vpc = network_stack.require_output("operations_vpc")
data_vpc = network_stack.require_output("data_vpc")
superset_env = f"data-{stack_info.env_suffix}"
superset_vault_kv_path = vault_mount_stack.require_output("superset_kv")["path"]
aws_config = AWSBase(tags={"OU": "data", "Environment": superset_env})

# Kubernetes provider setup
# mypy/pylance: Output[str] is acceptable at runtime
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
superset_namespace = "superset"
# Validate namespace exists in the cluster (declared by EKS stack)
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(superset_namespace, ns)
)
# Use a valid, existing service enum for labels
k8s_labels = K8sGlobalLabels(
    service=Services.superset,
    ou=BusinessUnit.data,
    stack=stack_info,
)
k8s_global_labels = k8s_labels.model_dump()

aws_account = get_caller_identity()
superset_domain = superset_config.require("domain")
superset_mail_domain = f"mail.{superset_domain}"

# S3 policy for Superset pods (IRSA)
superset_bucket_name = f"ol-superset-{stack_info.env_suffix}"
superset_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket*",
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject*",
            ],
            "Resource": [
                f"arn:aws:s3:::{superset_bucket_name}",
                f"arn:aws:s3:::{superset_bucket_name}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["ses:SendEmail", "ses:SendRawEmail"],
            "Resource": [
                "arn:*:ses:*:*:identity/*mit.edu",
                f"arn:aws:ses:*:*:configuration-set/superset-{superset_env}",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["ses:GetSendQuota"],
            "Resource": "*",
        },
    ],
}

superset_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="superset",
        namespace=superset_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=superset_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("superset_server_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="superset",
        vault_sync_service_account_names="superset-vault",
        k8s_labels=k8s_labels,
    )
)

superset_secrets = read_yaml_secrets(
    Path(f"superset/data.{stack_info.env_suffix}.yaml")
)
for path, data in superset_secrets.items():
    vault.kv.SecretV2(
        f"superset-vault-secret-{path}",
        mount=superset_vault_kv_path,
        name=path,
        data_json=json.dumps(data),
    )

########################################
# Create SES Service For superset Emails #
########################################

superset_ses_domain_identity = ses.DomainIdentity(
    "superset-ses-domain-identity",
    domain=superset_mail_domain,
)
superset_ses_verification_record = route53.Record(
    "superset-ses-domain-identity-verification-dns-record",
    zone_id=mitol_zone_id,
    name=superset_ses_domain_identity.id.apply("_amazonses.{}".format),
    type="TXT",
    ttl=FIVE_MINUTES,
    records=[superset_ses_domain_identity.verification_token],
)
superset_ses_domain_identity_verification = ses.DomainIdentityVerification(
    "superset-ses-domain-identity-verification-resource",
    domain=superset_ses_domain_identity.id,
    opts=ResourceOptions(depends_on=[superset_ses_verification_record]),
)
superset_mail_from_domain = ses.MailFrom(
    "superset-ses-mail-from-domain",
    domain=superset_ses_domain_identity_verification.domain,
    mail_from_domain=superset_ses_domain_identity_verification.domain.apply(
        "bounce.{}".format
    ),
)
superset_mail_from_address = ses.EmailIdentity(
    "superset-ses-mail-from-identity",
    email=superset_config.require("sender_email_address"),
)
# Example Route53 MX record
superset_ses_domain_mail_from_mx = route53.Record(
    f"superset-ses-mail-from-mx-record-for-{superset_env}",
    zone_id=mitol_zone_id,
    name=superset_mail_from_domain.mail_from_domain,
    type="MX",
    ttl=FIVE_MINUTES,
    records=["10 feedback-smtp.us-east-1.amazonses.com"],
)
ses_domain_mail_from_txt = route53.Record(
    "superset-ses-domain-mail-from-text-record",
    zone_id=mitol_zone_id,
    name=superset_mail_from_domain.mail_from_domain,
    type="TXT",
    ttl=FIVE_MINUTES,
    records=["v=spf1 include:amazonses.com -all"],
)
superset_ses_domain_dkim = ses.DomainDkim(
    "superset-ses-domain-dkim", domain=superset_ses_domain_identity.domain
)
for loop_counter in range(3):
    route53.Record(
        f"superset-ses-domain-dkim-record-{loop_counter}",
        zone_id=mitol_zone_id,
        name=superset_ses_domain_dkim.dkim_tokens[loop_counter].apply(
            lambda dkim_name: f"{dkim_name}._domainkey.{superset_mail_domain}"
        ),
        type="CNAME",
        ttl=FIVE_MINUTES,
        records=[
            superset_ses_domain_dkim.dkim_tokens[loop_counter].apply(
                "{}.dkim.amazonses.com".format
            )
        ],
    )
superset_ses_configuration_set = ses.ConfigurationSet(
    "superset-ses-configuration-set",
    reputation_metrics_enabled=True,
    sending_enabled=True,
    name=f"superset-{superset_env}",
)
superset_ses_event_destintations = ses.EventDestination(
    "superset-ses-event-destination-routing",
    configuration_set_name=superset_ses_configuration_set.name,
    enabled=True,
    matching_types=[
        "send",
        "reject",
        "bounce",
        "complaint",
        "delivery",
        "open",
        "click",
        "renderingFailure",
    ],
    cloudwatch_destinations=[
        ses.EventDestinationCloudwatchDestinationArgs(
            default_value="default",
            dimension_name=f"superset-{superset_env}",
            value_source="emailHeader",
        )
    ],
)

# Create RDS Postgres instance and connect with Vault
k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]

superset_db_security_group = ec2.SecurityGroup(
    "superset-rds-security-group",
    name_prefix=f"superset-rds-{superset_env}-",
    description="Grant access to RDS from Superset",
    ingress=[
        # Allow from k8s pod subnets
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Grant access to RDS from EKS pods",
        ),
        # Allow Vault as well
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[
                vault_infra_stack.require_output("vault_server")["security_group"]
            ],
            description="Grant access to RDS from Vault",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"superset-rds-{superset_env}"}),
    vpc_id=data_vpc["id"],
)
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = False
rds_defaults["read_replica"] = None
superset_db_config = OLPostgresDBConfig(
    instance_name=f"ol-superset-db-{stack_info.env_suffix}",
    password=superset_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[superset_db_security_group],
    tags=aws_config.tags,
    db_name="superset",
    **rds_defaults,
)
superset_db = OLAmazonDB(superset_db_config)

superset_vault_db_config = OLVaultPostgresDatabaseConfig(
    db_name=superset_db_config.db_name,
    mount_point=f"{superset_db_config.engine}-superset",
    db_admin_username=superset_db_config.username,
    db_admin_password=superset_config.require("db_password"),
    db_host=superset_db.db_instance.address,
)
superset_db_vault_backend = OLVaultDatabaseBackend(superset_vault_db_config)


# Create an Elasticache cluster for Redis caching and Celery broker
redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"superset-redis-cluster-{superset_env}",
    name_prefix=f"superset-redis-{superset_env}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        # Allow from EKS pods
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            cidr_blocks=k8s_pod_subnet_cidrs,
            description=(
                "Allow access from EKS pods to Redis for caching and queueing"
            ),
        ),
        # Allow from celery monitoring pods in Operations VPC
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            cidr_blocks=operations_vpc["k8s_pod_subnet_cidrs"],
            description=(
                "Allow access from Operations VPC celery monitoring pods to Redis"
            ),
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"superset-redis-{superset_env}"}),
    vpc_id=data_vpc["id"],
)

redis_instance_type = (
    redis_config.get("instance_type") or defaults(stack_info)["redis"]["instance_type"]
)
redis_auth_token = superset_secrets["redis"]["token"]
redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_auth_token,
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="7.2",
    engine="valkey",
    instance_type=redis_instance_type,
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"superset-redis-{superset_env}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=data_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
)
superset_redis_cache = OLAmazonCache(redis_cache_config)

########################################
# Vault Secrets synced into Kubernetes  #
########################################

# K8s/Vault resources (service account + VaultAuth/Connection)
vault_k8s_resources = superset_app.vault_k8s_resources

# DB dynamic creds secret for Superset
db_creds_secret_name = "superset-db-creds"  # pragma: allowlist secret  # noqa: S105
db_creds_secret = Output.all(
    address=superset_db.db_instance.address,
    port=superset_db.db_instance.port,
    db_name=superset_db.db_instance.db_name,
).apply(
    lambda db: OLVaultK8SSecret(
        f"superset-db-creds-{stack_info.env_suffix}",
        OLVaultK8SDynamicSecretConfig(
            name="superset-db-creds",
            namespace=superset_namespace,
            labels=k8s_global_labels,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=db_creds_secret_name,
            mount=superset_db_vault_backend.db_mount.path,
            path="creds/app",
            exclude_raw=True,
            templates={
                "DB_USER": "{{ .Secrets.username }}",
                "DB_PASS": "{{ .Secrets.password }}",
                "DB_NAME": f"{db['db_name']}",
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=vault_k8s_resources,
        ),
    )
)

# Redis password from KV -> Kubernetes Secret
redis_secret_name = "superset-redis-secret"  # pragma: allowlist secret  # noqa: S105
redis_password_secret = OLVaultK8SSecret(
    f"superset-redis-password-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="superset-redis-token",
        namespace=superset_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=redis_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=superset_vault_kv_path,
        mount_type="kv-v2",
        path="redis",
        templates={
            "REDIS_PASSWORD": '{{ get .Secrets "token" }}',
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
)

# App configuration (SECRET_KEY, optional Slack token) from Vault KV
app_config_secret_name = "superset-app-secret"  # pragma: allowlist secret # noqa: S105
# The DB and Redis host/port combos need to be in this secret so that the init
# containers running Dockerize have the values in their environment at startup.
app_config_secret = Output.all(
    db_host=superset_db.db_instance.address,
    db_port=superset_db.db_instance.port,
    redis_host=superset_redis_cache.address,
).apply(
    lambda kwargs: OLVaultK8SSecret(
        f"superset-app-config-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            name="superset-app-config",
            namespace=superset_namespace,
            labels=k8s_global_labels,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=app_config_secret_name,
            dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
            mount=superset_vault_kv_path,
            mount_type="kv-v2",
            path="app-config",
            templates={
                "SECRET_KEY": '{{ get .Secrets "secret_key" }}',
                # Optional Slack token for Alerts & Reports
                "SLACK_API_TOKEN": '{{ get .Secrets "slack_token" | default "" }}',
                "DB_PORT": str(kwargs["db_port"]),
                "DB_HOST": kwargs["db_host"],
                "REDIS_HOST": kwargs["redis_host"],
                "REDIS_PORT": str(DEFAULT_REDIS_PORT),
            },
            refresh_after="1h",
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=vault_k8s_resources
        ),
    )
)

# OIDC (Keycloak) config from operations KV -> Kubernetes Secret env
oidc_secret_name = "superset-oidc-secret"  # pragma: allowlist secret  # noqa: S105
oidc_secret = OLVaultK8SSecret(
    f"superset-oidc-config-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="superset-oidc-config",
        namespace=superset_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=oidc_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/superset",
        templates={
            "OIDC_URL": '{{ get .Secrets "url" }}',
            "OIDC_CLIENT_ID": '{{ get .Secrets "client_id" }}',
            "OIDC_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            "OIDC_REALM_PUBLIC_KEY": '{{ get .Secrets "realm_public_key" }}',
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
)

########################################
# Superset Helm chart on Kubernetes    #
########################################

superset_chart = kubernetes.helm.v3.Release(
    "superset-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="superset",
        chart="superset",
        version=SUPERSET_CHART_VERSION,
        namespace=superset_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://apache.github.io/superset",
        ),
        values={
            "fullnameOverride": "superset",
            "image": {
                # Use our custom image that bundles config and deps
                "repository": cached_image_uri("mitodl/superset"),
                "tag": "latest",
            },
            # Bring your own Postgres/Redis
            "postgresql": {"enabled": False},
            "redis": {"enabled": False},
            # Do not let the chart create its own env secret; use Vault-synced ones
            "secretEnv": {"create": False},
            "extraEnv": {
                "REDIS_PROTO": "rediss",
            },
            "configOverrides": {
                "config": Path(__file__)
                .parent.joinpath("superset_config.py")
                .read_text()
            },
            "envFromSecret": app_config_secret_name,
            "envFromSecrets": [
                redis_secret_name,
                db_creds_secret_name,
                oidc_secret_name,
            ],
            # Connections (non-secret parts)
            "supersetNode": {
                "podLabels": k8s_global_labels,
                "connections": {
                    "redis_host": superset_redis_cache.address,
                    "redis_port": str(DEFAULT_REDIS_PORT),
                    "db_name": superset_db_config.db_name,
                    "db_port": str(DEFAULT_POSTGRES_PORT),
                    "db_host": superset_db.db_instance.address,
                },
                "replicas": {"enabled": False},
                "autoscaling": {
                    "enabled": True,
                    "targetCPUUtilizationPercentage": "60",
                    "targetMemoryUtilizationPercentage": "80",
                },
                "resources": {
                    "limits": {"cpu": "2000m", "memory": "2Gi"},
                    "requests": {"cpu": "500m", "memory": "768Mi"},
                },
            },
            "supersetWorker": {
                "podLabels": k8s_global_labels,
                "replicas": {"enabled": True, "replicaCount": 1},
            },
            "supersetCeleryBeat": {
                "enabled": True,
                "podLabels": k8s_global_labels,
            },
            "serviceAccount": {
                "create": True,
                "name": "superset",
                "annotations": {
                    "eks.amazonaws.com/role-arn": superset_app.irsa_role.arn.apply(
                        lambda arn: f"{arn}"
                    ),
                },
            },
            # Disable chart's ingress; we'll attach Gateway API below
            "ingress": {"enabled": False},
        },
    ),
)

celery_keda_scaling = kubernetes.apiextensions.CustomResource(
    "superset-celery-worker-scaledobject",
    api_version="keda.sh/v1alpha1",
    kind="ScaledObject",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="superset-celery",
        namespace=superset_namespace,
        labels=k8s_global_labels
        | {"ol.mit.edu/process": "celery-worker", "ol.mit.edu/worker-name": "default"},
    ),
    spec=Output.all(
        address=superset_redis_cache.cache_cluster.primary_endpoint_address,
        token=superset_redis_cache.cache_cluster.auth_token,
    ).apply(
        lambda cache: {
            "scaleTargetRef": {
                "kind": "Deployment",
                "name": "superset-worker",
            },
            "pollingInterval": 3,
            "cooldownPeriod": 10,
            "maxReplicaCount": 10,
            "minReplicaCount": 1,
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": f"{cache['address']}:{DEFAULT_REDIS_PORT}",
                        "username": "default",
                        "databaseIndex": "0",
                        "password": cache["token"],
                        "listName": "celery",
                        "listLength": "5",
                        "enableTLS": "true",
                    },
                },
            ],
        }
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

########################################
# Gateway API routing + TLS certificate #
########################################

gateway_config = OLEKSGatewayConfig(
    cert_issuer="letsencrypt-production",
    cert_issuer_class="cluster-issuer",
    gateway_name="superset",
    namespace=superset_namespace,
    listeners=[
        OLEKSGatewayListenerConfig(
            name="https-web",
            hostname=superset_domain,
            port=8443,
            tls_mode="Terminate",
            certificate_secret_name="superset-tls",  # pragma: allowlist secret  # noqa: E501, S106
            certificate_secret_namespace=superset_namespace,
        ),
    ],
    routes=[
        OLEKSGatewayRouteConfig(
            backend_service_name="superset",
            backend_service_namespace=superset_namespace,
            backend_service_port=8088,
            name="superset-https-root",
            listener_name="https-web",
            hostnames=[superset_domain],
            port=8443,
            matches=[{"path": {"type": "PathPrefix", "value": "/"}}],
        ),
    ],
)

_gateway = OLEKSGateway(
    "superset-gateway",
    gateway_config=gateway_config,
    opts=ResourceOptions(parent=superset_chart, depends_on=[superset_chart]),
)
# DNS is managed at the Gateway/ingress layer via cert-manager/external-dns

export(
    "superset",
    {
        "deployment": stack_info.env_prefix,
        "redis": superset_redis_cache.address,
        "redis_token": redis_auth_token,
    },
)
