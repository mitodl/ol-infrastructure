from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_MYSQL_PORT,
)
from bridge.lib.versions import OPEN_METADATA_VERSION
from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

open_metadata_config = Config("open_metadata")
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.open_metadata.{stack_info.name}"
)
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

opensearch_cluster = opensearch_stack.require_output("cluster")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]
open_metadata_environment = f"operations-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": open_metadata_environment},
)

vault_config = Config("vault")

consul_provider = get_consul_provider(stack_info)
setup_vault_provider(stack_info)
k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
}
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster_stack.require_output("kube_config"),
)

consul_security_groups = consul_stack.require_output("security_groups")
aws_account = get_caller_identity()

open_metadata_namespace = "open-metadata"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(open_metadata_namespace, ns)
)

open_metadata_database_security_group = ec2.SecurityGroup(
    f"open-metadata-database-security-group-{stack_info.env_suffix}",
    name=f"open-metadata-database-security-group-{stack_info.env_suffix}",
    description="Access control for the open metadata database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                # add k8s ingress?
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            description="Access to postgres from open metadata servers.",
        ),
        # TODO @Ardiea: switch to use pod-security-groups once implemented
        ec2.SecurityGroupIngressArgs(
            security_groups=[],
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster to talk to DB",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    open_metadata_config.get("db_instance_size") or DBInstanceTypes.small.value
)

open_metadata_db_config = OLMariaDBConfig(
    instance_name=f"open-metadata-db-{stack_info.env_suffix}",
    password=open_metadata_config.get("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[open_metadata_database_security_group],
    storage=open_metadata_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    engine_major_version="16",
    tags=aws_config.tags,
    db_name="open_metadata",
    **defaults(stack_info)["rds"],
)
open_metadata_db = OLAmazonDB(open_metadata_db_config)

open_metadata_db_vault_backend_config = OLVaultMysqlDatabaseConfig(
    db_name=open_metadata_db_config.db_name,
    mount_point=f"{open_metadata_db_config.engine}-open-metadata",
    db_admin_username=open_metadata_db_config.username,
    db_admin_password=open_metadata_config.get("db_password"),
    db_host=open_metadata_db.db_instance.address,
)
open_metadata_db_vault_backend = OLVaultDatabaseBackend(
    open_metadata_db_vault_backend_config
)

open_metadata_db_consul_node = Node(
    "open-metadata-postgres-db",
    name="open-metadata-postgres-db",
    address=open_metadata_db.db_instance.address,
    opts=consul_provider,
)

open_metadata_db_consul_service = Service(
    "open-metadata-instance-db-service",
    node=open_metadata_db_consul_node.name,
    name="open-metadata-db",
    port=open_metadata_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="open-metadata-instance-db",
            interval="10s",
            name="open-metadata-instance-id",
            timeout="60s",
            status="passing",
            tcp=open_metadata_db.db_instance.address.apply(
                lambda address: f"{address}:{open_metadata_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)

## Begin block for migrating to pyinfra images
consul_datacenter = consul_stack.require_output("datacenter")

# Pulumi k8s omd help stuff. Still struggling with syntax.

cluster_name = cluster_stack.require_output("cluster_name")


# Create a vault policy and associate it with an auth backend role
# on the vault k8s cluster auth endpoint
open_metadata_vault_policy = vault.Policy(
    "open-metadata-vault-policy",
    name="open-metadata",
    policy=Path(__file__).parent.joinpath("open_metadata_policy.hcl").read_text(),
)
open_metadata_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    "open-metadata-vault-k8s-auth-backend-role",
    role_name="open-metadata",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[open_metadata_namespace],
    token_policies=[open_metadata_vault_policy.name],
)

# TODO @Ardiea Add k8s global labels
vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="open-metadata",
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=open_metadata_vault_auth_backend_role.role_name,
    k8s_namespace=open_metadata_namespace,
    k8s_provider=k8s_provider,
    k8s_global_labels=k8s_global_labels,
)
vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        parent=k8s_provider, depends_on=[open_metadata_vault_auth_backend_role]
    ),
)

db_creds_secret_name = "pgsql-db-creds"
db_creds_dynamic_secret = kubernetes.yaml.v2.ConfigGroup(
    "open-metadata-dynamicsecret-db-creds",
    objs=[
        {
            "apiVersion": "secrets.hashicorp.com/v1beta1",
            "kind": "VaultDynamicSecret",
            "metadata": {
                "name": "openmetadata-db-credentials",
                "namespace": open_metadata_namespace,
                "labels": k8s_global_labels,
            },
            "spec": {
                "mount": open_metadata_db_vault_backend_config.mount_point,
                "path": "creds/app",
                "destination": {
                    "create": True,
                    "overwrite": True,
                    "name": db_creds_secret_name,
                    "transformation": {
                        "excludes": [".*"],
                        "templates": {
                            "DB_USER": {
                                "text": '{{ get .Secrets ".username" }}',
                            },
                            "DB_PASSWORD": {
                                "text": '{{ get .Secrets ".password" }}',
                            },
                        },
                    },
                },
                "rolloutRestartTargets": [
                    {
                        "kind": "Deployment",
                        # Name of the 'deployment' from the OMD helm chart
                        "name": "openmetadata",
                    },
                ],
                "vaultAuthRef": vault_k8s_resources.auth_name,
            },
        },
    ],
    opts=ResourceOptions(
        provider=k8s_provider, parent=k8s_provider, depends_on=[vault_k8s_resources]
    ),
)

# Install the openmetadata helm chart
# TODO @Ardiea Add k8s global labels
# https://github.com/mitodl/ol-infrastructure/issues/2680
open_metadata_application = kubernetes.helm.v3.Release(
    f"open-metadata-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="open-metadata",
        chart="openmetadata",
        version=OPEN_METADATA_VERSION,
        namespace=open_metadata_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://helm.open-metadata.org",
        ),
        values={
            "commonLabels": k8s_global_labels,
            "openmetadata": {
                "config": {
                    "elasticsearch": {
                        "host": opensearch_cluster["endpoint"],
                        "port": DEFAULT_HTTPS_PORT,
                    },
                    "database": {
                        "host": open_metadata_db.db_instance.address,
                        "port": open_metadata_db_config.port,
                        "databaseName": open_metadata_db_config.db_name,
                        # Null out the auth elements in favor of our own
                        # secret pulled in with envFrom below.
                        "auth": {
                            "username": "",
                            "password": {
                                "secretRef": "",
                                "secretKey": "",
                            },
                        },
                    },
                },
            },
            "envFrom": [
                {
                    "secretRef": {
                        "name": db_creds_secret_name,
                    },
                },
            ],
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        depends_on=[open_metadata_db, db_creds_dynamic_secret],
    ),
)
