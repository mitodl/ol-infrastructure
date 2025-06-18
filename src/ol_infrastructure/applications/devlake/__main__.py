"""Pulumi program for deploying Apache DevLake to a Kubernetes cluster."""

from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import ec2
from pulumi_kubernetes import Provider
from pulumi_kubernetes.apiextensions.v1 import CustomResource
from pulumi_kubernetes.core.v1 import Namespace
from pulumi_kubernetes.helm.v3 import Release, ReleaseArgs, RepositoryOptsArgs
from pulumi_vault import Mount
from pulumi_vault.database import SecretBackendConnection, SecretBackendRole

from ol_infrastructure.components.aws.database import OLAmazonDB, OLMySQLDBConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
devlake_config = Config("devlake")
db_password = devlake_config.require_secret("db_password")
devlake_encryption_secret = devlake_config.require_secret("encryption_secret")
grafana_admin_password = devlake_config.require_secret("grafana_admin_password")

# Stack References
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
data_vpc = network_stack.require_output("data_vpc")
aws_config = AWSBase(
    tags={
        "OU": "operations",
        "business_unit": "operations",
        "team": "infrastructure",
    }
)

ops_kubernetes_stack = StackReference(
    f"infrastructure.kubernetes.operations.{stack_info.name}"
)
k8s_provider = Provider(
    "k8s-provider",
    kubeconfig=ops_kubernetes_stack.require_output("kubeconfig"),
)

vault_provider = setup_vault_provider()

# Namespace for DevLake
devlake_namespace = Namespace(
    "devlake-namespace",
    metadata={"name": "devlake"},
    opts=ResourceOptions(provider=k8s_provider),
)

# Database
# Security Group for DevLake DB
devlake_db_security_group = ec2.SecurityGroup(
    f"devlake-db-access-{stack_info.env_suffix}",
    name=f"devlake-db-access-{stack_info.env_suffix}",
    description="Access control for the DevLake database",
    vpc_id=data_vpc["id"],
    tags=aws_config.merged_tags({"Name": f"devlake-db-access-{stack_info.env_suffix}"}),
)

# Allow access from the k8s cluster
ec2.SecurityGroupRule(
    f"devlake-db-access-from-k8s-{stack_info.env_suffix}",
    security_group_id=devlake_db_security_group.id,
    type="ingress",
    from_port=3306,
    to_port=3306,
    protocol="tcp",
    source_security_group_id=ops_kubernetes_stack.require_output(
        "cluster_security_group_id"
    ),
    description="Allow access from the Kubernetes cluster",
)

# DB Config
devlake_db_config = OLMySQLDBConfig(
    instance_name=f"devlake-db-{stack_info.env_suffix}",
    password=db_password,
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[devlake_db_security_group],
    storage=20,  # Starting with 20GB, can be adjusted
    tags=aws_config.tags,
    db_name="devlake",
    engine="mysql",
    engine_version="8.0.35",  # A recent version of MySQL 8.0
    **defaults(stack_info)["rds"],
)

# DB instance
devlake_db = OLAmazonDB(devlake_db_config)

# Vault Database Backend for DevLake
db_backend_path = "mysql-devlake"
db_backend_role_name = "app"
db_backend = Mount(
    f"{db_backend_path}-backend",
    path=db_backend_path,
    type="database",
    description="Vault backend for DevLake MySQL database",
    opts=ResourceOptions(provider=vault_provider),
)
db_connection = SecretBackendConnection(
    f"{db_backend_path}-connection",
    backend=db_backend.path,
    name=db_backend_path,
    allowed_roles=[db_backend_role_name],
    mysql={
        "connection_url": devlake_db.db_instance.address.apply(
            lambda address: (
                f"{devlake_db_config.username}:{db_password.get_secret_value()}@"
                f"tcp({address}:3306)/{devlake_db_config.db_name}"
            )
        )
    },
    opts=ResourceOptions(provider=vault_provider),
)
db_role = SecretBackendRole(
    f"{db_backend_path}-role-{db_backend_role_name}",
    backend=db_backend.path,
    name=db_backend_role_name,
    db_name=db_connection.name,
    creation_statements=[
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
        f"GRANT ALL PRIVILEGES ON {devlake_db_config.db_name}.* TO '{{{{name}}}}'@'%';",
    ],
    default_ttl=3600,  # 1 hour
    max_ttl=86400,  # 24 hours
    opts=ResourceOptions(provider=vault_provider),
)

# VaultDynamicSecret for DB credentials
db_k8s_secret_name = "devlake-db-credentials"  # noqa: S105
url_params = "parseTime=true&loc=UTC"
db_url_template = devlake_db.db_instance.address.apply(
    lambda address: (
        f"mysql://{{{{ .username }}}}:{{{{ .password }}}}@{address}:3306/"
        f"{devlake_db_config.db_name}?{url_params}"
    )
)
db_dynamic_secret = CustomResource(
    "devlake-db-dynamic-secret",
    api_version="secrets.hashicorp.com/v1beta1",
    kind="VaultDynamicSecret",
    metadata={
        "name": "devlake-db",
        "namespace": devlake_namespace.metadata["name"],
    },
    spec={
        "mount": db_backend.path,
        "role": db_backend_role_name,
        "destination": {
            "name": db_k8s_secret_name,
            "create": True,
            "template": {"db_url": db_url_template},
        },
    },
    opts=ResourceOptions(provider=k8s_provider, depends_on=[db_role]),
)

# Static secrets in Vault
static_secrets_path = f"secret-operations/data/{stack_info.env_suffix}/devlake"
static_secrets = Mount(
    "devlake-static-secrets",
    path=static_secrets_path,
    type="kv",
    options={"version": "2"},
    opts=ResourceOptions(provider=vault_provider),
)

# VaultStaticSecret for encryption key
enc_k8s_secret_name = "devlake-encryption-secret"  # noqa: S105
encryption_secret_in_vault = CustomResource(
    "devlake-encryption-secret-in-vault",
    api_version="secrets.hashicorp.com/v1beta1",
    kind="VaultStaticSecret",
    metadata={
        "name": "devlake-encryption",
        "namespace": devlake_namespace.metadata["name"],
    },
    spec={
        "mount": "secret",
        "path": f"operations/{stack_info.env_suffix}/devlake",
        "type": "kv-v2",
        "destination": {"name": enc_k8s_secret_name, "create": True},
        "refreshAfter": "1h",
    },
    opts=ResourceOptions(provider=k8s_provider),
)

# VaultStaticSecret for Grafana password
grafana_k8s_secret_name = "devlake-grafana-password"  # noqa: S105
grafana_password_in_vault = CustomResource(
    "devlake-grafana-password-in-vault",
    api_version="secrets.hashicorp.com/v1beta1",
    kind="VaultStaticSecret",
    metadata={
        "name": "devlake-grafana",
        "namespace": devlake_namespace.metadata["name"],
    },
    spec={
        "mount": "secret",
        "path": f"operations/{stack_info.env_suffix}/devlake",
        "type": "kv-v2",
        "destination": {"name": grafana_k8s_secret_name, "create": True},
        "refreshAfter": "1h",
    },
    opts=ResourceOptions(provider=k8s_provider),
)

# Helm Chart for DevLake
devlake_domain = f"devlake.operations.{stack_info.env_suffix}.mitodl.net"
cert_secret_name = "devlake-operations-tls"  # noqa: S105

# TLS Certificate
devlake_cert = OLCertManagerCert(
    "devlake-certificate",
    OLCertManagerCertConfig(
        cert_name="devlake-cert",
        namespace=devlake_namespace.metadata["name"],
        domain=devlake_domain,
        secret_name=cert_secret_name,
    ),
    opts=ResourceOptions(provider=k8s_provider),
)

# Helm Release
devlake_release = Release(
    "devlake-helm-release",
    ReleaseArgs(
        chart="devlake",
        repository_opts=RepositoryOptsArgs(
            repo="https://apache.github.io/incubator-devlake-helm-chart"
        ),
        namespace=devlake_namespace.metadata["name"],
        version="0.21.0",  # Specify a version for reproducibility
        values={
            "dbUrl": "mysql://dummy:dummy@dummy/dummy",  # Overridden by extraEnvs
            "lake": {
                "encryptionSecret": {
                    "secretName": enc_k8s_secret_name,
                    "secretKey": "encryption_secret",
                },
                "extraEnvs": [
                    {
                        "name": "DB_URL",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": db_k8s_secret_name,
                                "key": "db_url",
                            }
                        },
                    }
                ],
            },
            "api": {
                "extraEnvs": [
                    {
                        "name": "DB_URL",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": db_k8s_secret_name,
                                "key": "db_url",
                            }
                        },
                    }
                ]
            },
            "grafana": {
                "adminPassword": "",  # Use existingSecret instead
                "existingSecret": grafana_k8s_secret_name,
                "existingSecretPasswordKey": "grafana_admin_password",
                "env": {"TZ": "America/New_York"},
            },
            "ingress": {
                "enabled": True,
                "className": "traefik",
                "hostname": devlake_domain,
                "annotations": {
                    "cert-manager.io/cluster-issuer": "letsencrypt-production",
                    "traefik.ingress.kubernetes.io/router.entrypoints": "web,websecure",
                    "traefik.ingress.kubernetes.io/router.tls": "true",
                },
                "tls": [{"secretName": cert_secret_name, "hosts": [devlake_domain]}],
            },
            "commonEnvs": {"TZ": "America/New_York"},
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[
            devlake_cert,
            db_dynamic_secret,
            encryption_secret_in_vault,
            grafana_password_in_vault,
        ],
    ),
)

# Exports
export("devlake_db_endpoint", devlake_db.db_instance.address)
