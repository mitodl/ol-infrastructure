# ruff: noqa: ERA001, C416

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_POSTGRES_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.cache_helper import CacheInstanceTypes
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

ecommerce_config = Config("ecommerce")
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")

apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
ecommerce_environment = f"applications-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": ecommerce_environment},
)
vault_config = Config("vault")

consul_provider = get_consul_provider(stack_info)
setup_vault_provider(stack_info)
k8s_global_labels = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/service": "unified-ecommerce",
}
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster_stack.require_output("kube_config"),
)

consul_security_groups = consul_stack.require_output("security_groups")
aws_account = get_caller_identity()

UNIFIED_ECOMMERCE_LISTENER_PORT = "8073"

ecommerce_namespace = "ecommerce"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(ecommerce_namespace, ns)
)

# Put the application secrets into vault
ecommerce_vault_secrets = read_yaml_secrets(
    Path(f"unified_ecommerce/secrets.{stack_info.env_suffix}.yaml"),
)
ecommerce_vault_mount = vault.Mount(
    f"unified-ecommerce-secrets-mount-{stack_info.env_suffix}",
    path="secrets-ecommerce",
    type="kv-v2",
    options={"version": "2"},
    description="Secrets for the unified ecommerce application.",
    opts=ResourceOptions(delete_before_replace=True),
)
ecommerce_static_vault_secrets = vault.generic.Secret(
    f"unified-ecommerce-secrets-{stack_info.env_suffix}",
    path=ecommerce_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(ecommerce_vault_secrets),
)

# Security Group
ecommerce_database_security_group = ec2.SecurityGroup(
    f"unified-ecommerce-db-security-group-{stack_info.env_suffix}",
    name=f"unified-ecommerce-db-security-group-{stack_info.env_suffix}",
    description="Access control for the unified ecommerce database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to postgres from consul and vault.",
        ),
        # TODO @Ardiea: switch to use pod-security-groups once implemented  # noqa: TD003, FIX002, E501
        ec2.SecurityGroupIngressArgs(
            security_groups=[],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster to talk to DB",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    ecommerce_config.get("db_instance_size") or DBInstanceTypes.small.value
)

ecommerce_db_config = OLPostgresDBConfig(
    instance_name=f"unified-ecommerce-db-{stack_info.env_suffix}",
    password=ecommerce_config.get("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[ecommerce_database_security_group],
    storage=ecommerce_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    engine_major_version="16",
    tags=aws_config.tags,
    db_name="ecommerce",
    **defaults(stack_info)["rds"],
)
ecommerce_db = OLAmazonDB(ecommerce_db_config)

ecommerce_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ecommerce_db_config.db_name,
    mount_point=f"{ecommerce_db_config.engine}-ecommerce",
    db_admin_username=ecommerce_db_config.username,
    db_admin_password=ecommerce_config.get("db_password"),
    db_host=ecommerce_db.db_instance.address,
)
ecommerce_db_vault_backend = OLVaultDatabaseBackend(
    ecommerce_db_vault_backend_config,
    opts=ResourceOptions(delete_before_replace=True, parent=ecommerce_db),
)

# A bunch of RDS + Consul stuff of questionable utility
ecommerce_db_consul_node = Node(
    f"unified-ecommerce-{stack_info.env_suffix}-postgres-db",
    name="ecommerce-postgres-db",
    address=ecommerce_db.db_instance.address,
    opts=consul_provider,
)

ecommerce_db_consul_service = Service(
    "ecommerce-instance-db-service",
    node=ecommerce_db_consul_node.name,
    name="ecommerce-db",
    port=ecommerce_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="ecommerce-instance-db",
            interval="10s",
            name="ecommerce-instance-id",
            timeout="60s",
            status="passing",
            tcp=ecommerce_db.db_instance.address.apply(
                lambda address: f"{address}:{ecommerce_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)

# Redis
redis_config = Config("redis")
redis_instance_type = (
    redis_config.get("instance_type") or CacheInstanceTypes.micro.value
)

redis_cluster_security_group = ec2.SecurityGroup(
    f"unified-ecommerce-redis-cluster-security-group-{stack_info.env_suffix}",
    name_prefix=f"unified-ecommerce-{stack_info.env_suffix}-",
    description="Access control for the unified ecommerce redis cluster.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[],
            protocol="tcp",
            from_port=6379,
            to_port=6379,
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster to talk to redis",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("password"),
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="6.2",
    instance_type=redis_instance_type,
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for unified-ecommerce tasks and caching.",
    cluster_name=f"unified-ecommerce-redis-{stack_info.env_suffix}",
    subnet_group=apps_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
)
redis_cache = OLAmazonCache(redis_cache_config)

# Create a vault policy and associate it with an auth backend role
# on the vault k8s cluster auth endpoint
ecommerce_vault_policy = vault.Policy(
    f"unified-ecommerce-{stack_info.env_suffix}-vault-policy",
    name="unified-ecommerce",
    policy=Path(__file__).parent.joinpath("ecommerce_policy.hcl").read_text(),
)
ecommerce_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    "ecommerce-vault-k8s-auth-backend-role",
    role_name="unified-ecommerce",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[ecommerce_namespace],
    token_policies=[ecommerce_vault_policy.name],
)

vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="unified-ecommerce",
    namespace=ecommerce_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=ecommerce_vault_auth_backend_role.role_name,
)
vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[ecommerce_vault_auth_backend_role],
    ),
)

db_creds_secret_name = "pgsql-db-creds"  # noqa: S105  # pragma: allowlist secret
db_creds_secret = Output.all(address=ecommerce_db.db_instance.address).apply(
    lambda db: OLVaultK8SSecret(
        f"unified-ecommerce-{stack_info.name}-db-creds-secret",
        OLVaultK8SDynamicSecretConfig(
            name="ecommerce-db-creds",
            namespace=ecommerce_namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=db_creds_secret_name,
            labels=k8s_global_labels,
            mount=ecommerce_db_vault_backend_config.mount_point,
            path="creds/app",
            restart_target_kind="Deployment",
            restart_target_name="ecommerce-app",
            templates={
                "DATABASE_URL": f'postgres://{{{{ get .Secrets "username"}}}}:{{{{ get .Secrets "password" }}}}@{db["address"]}:{ecommerce_db_config.port}/{ecommerce_db_config.db_name}',  # noqa: E501
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
            parent=vault_k8s_resources,
            depends_on=[ecommerce_db_vault_backend],
        ),
    )
)

static_secrets_name = "ecommerce-static-secrets"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name="unified-ecommerce-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="unified-ecommerce-static-secrets",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secrets-ecommerce",
        mount_type="kv-v2",
        path="secrets",
        includes=["*"],
        excludes=[],
        exclude_raw=True,
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
        parent=vault_k8s_resources,
        depends_on=[ecommerce_static_vault_secrets],
    ),
)

misc_config = kubernetes.core.v1.ConfigMap(
    "ecommerce-misc-config",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ecommerce-misc-config",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    data={
        k: v for (k, v) in (ecommerce_config.require_object("env_vars") or {}).items()
    }
    | {
        "PORT": UNIFIED_ECOMMERCE_LISTENER_PORT,
    },
)

redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
redis_creds = kubernetes.core.v1.Secret(
    "unified-ecommerce-redis-creds",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=redis_creds_secret_name,
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    string_data={
        "CELERY_BROKER_URL": f'rediss://default:{redis_config.require("password")}@{redis_cache.address}:6379/0?ssl_cert_Reqs=required',
        "CELERY_RESULT_BACKEND": f'rediss://default:{redis_config.require("password")}@{redis_cache.address}:6379/0?ssl_cert_Reqs=required',
    },
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[redis_cache],
        delete_before_replace=True,
    ),
)

deployment_labels = k8s_global_labels | {"ol.mit.edu/application": "unified-ecommerce"}
ecommerce_deployment_resource = kubernetes.apps.v1.Deployment(
    f"unified-ecommerce-{stack_info.env_suffix}-deployment",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ecommerce-app",
        namespace=ecommerce_namespace,
        labels=deployment_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=deployment_labels,
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=deployment_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="ecommerce-app",
                        image="mitodl/unified-ecommerce-app-main:latest",
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(container_port=8071)
                        ],
                        image_pull_policy="Always",
                        env_from=[
                            # Database creds
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=db_creds_secret_name,
                                ),
                            ),
                            # Redis Configuration
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=redis_creds_secret_name,
                                ),
                            ),
                            # static secrets from secrets-ecommerce/secrets
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=static_secrets_name,
                                ),
                            ),
                            # vars specified in stack configs
                            kubernetes.core.v1.EnvFromSourceArgs(
                                config_map_ref=kubernetes.core.v1.ConfigMapEnvSourceArgs(
                                    name="ecommerce-misc-config",
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
        depends_on=[db_creds_secret, redis_creds, misc_config],
    ),
)

# gateway_config = OLEKSGatewayConfig(
#    cert_issuer="letsencrypt-production",
#    cert_issuer_class="cluster-issuer",
#    gateway_name="open-metadata",
#    labels=k8s_global_labels,
#    namespace=open_metadata_namespace,
#    listeners=[
#        OLEKSGatewayListenerConfig(
#            name="https",
#            hostname=open_metadata_config.require("domain"),
#            port=8443,
#            tls_mode="Terminate",
#            certificate_secret_name="openmetadata-tls",  # cert-manager will create this  # noqa: E501  # pragma: allowlist secret
#            certificate_secret_namespace=open_metadata_namespace,
#        ),
#    ],
#    routes=[
#        OLEKSGatewayRouteConfig(
#            backend_service_name="openmetadata",  # sourced from the helm chart
#            backend_service_namespace=open_metadata_namespace,
#            backend_service_port=8585,  # sourced from the helm chart
#            hostnames=[open_metadata_config.require("domain")],
#            name="open-metadata-https",
#            listener_name="https",
#            port=8443,
#        ),
#    ],
# )
#
# gateway = OLEKSGateway(
#    f"open-metadata-{stack_info.name}-gateway",
#    gateway_config=gateway_config,
#    opts=ResourceOptions(
#        provider=k8s_provider,
#        parent=open_metadata_application,
#        delete_before_replace=True,
#    ),
# )
