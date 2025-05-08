"""The complete state needed to provision Keycloak running on Docker."""

import json
from functools import partial
from pathlib import Path

# New Kubernetes imports
import pulumi_kubernetes as k8s
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import acm, ec2, get_caller_identity

# Import for Gateway API types
from pulumi_kubernetes.gateway.networking.v1 import Gateway, HTTPRoute
from pulumi_kubernetes.yaml import ConfigFile

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    # DEFAULT_HTTPS_PORT, # No longer directly used for Keycloak server SG
    DEFAULT_POSTGRES_PORT,
)
from bridge.secrets.sops import read_yaml_secrets

# Imports for OLAutoScaleGroupConfig are no longer used
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.route53_helper import acm_certificate_validation_records
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

keycloak_config = Config("keycloak")
stack_info = parse_stack()

aws_account = get_caller_identity()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
# Keep if used by other parts
vault_pki_stack = StackReference(f"substructure.vault.pki.operations.{stack_info.name}")

# target vpc is 'operations', for a non-app-specific service
target_vpc_name = keycloak_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]

data_vpc = network_stack.require_output("data_vpc")

mitol_zone_id = dns_stack.require_output("ol")["id"]
keycloak_domain = keycloak_config.get("domain")

# TODO MD 20230206  # noqa: FIX002, TD002, TD003, TD004
# This might be needed in the future but right now it just causes errors
secrets_path_str = keycloak_config.get("secrets_path") or (
    f"keycloak/data.{stack_info.env_suffix}.yaml"
)  # Ensure this path is correct
secrets = read_yaml_secrets(Path(secrets_path_str))
if secrets is None:
    msg = (
        f"You must create the secrets structure at"
        f" src/bridge/secrets/{secrets_path_str}"
    )
    raise ValueError(msg)

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# --- Kubernetes Provider Setup ---
# Assuming EKS cluster and kubeconfig are set up for Pulumi.
k8s_provider = k8s.Provider(f"keycloak-k8s-provider-{env_name}")


# --- Namespace for Keycloak ---
keycloak_namespace_name_config = keycloak_config.get("namespace") or "keycloak"
keycloak_namespace = k8s.core.v1.Namespace(
    f"keycloak-namespace-{env_name}",
    metadata={"name": keycloak_namespace_name_config},
    opts=ResourceOptions(provider=k8s_provider),
)
keycloak_namespace_name = keycloak_namespace.metadata["name"]

# Create various security groups
keycloak_server_security_group = ec2.SecurityGroup(
    f"keycloak-server-security-group-{env_name}",
    name=f"keycloak-server-{target_vpc_name}-{env_name}",
    description="Access control for keycloak servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                f"Allow traffic to the keycloak server on port {DEFAULT_HTTPS_PORT}"
            ),
        ),
        # https://infinispan.org/docs/stable/titles/security/security.html#ports_protocols
        ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=7800,
            to_port=7800,
            protocol="tcp",
            description=(
                "Allow all keycloak servers to talk to all other keycloak servers on"
                " port 7800 tcp for IPSN clustering."
            ),
        ),
        ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=7800,
            to_port=7800,
            protocol="udp",
            description=(
                "Allow all keycloak servers to talk to all other keycloak servers on"
                " port 7800 udp for IPSN clustering."
            ),
        ),
        ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=57800,
            to_port=57800,
            protocol="tcp",
            description=("Failure detection is provided by FD_SOCK2"),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

# --- IAM ---
# IAM Roles for Service Accounts (IRSA) would be configured here if Keycloak pods
# need AWS access (e.g., for an Ingress controller or future integrations).


# --- Network Access Control ---
# Modify keycloak_database_security_group to allow access from K8s worker nodes/pods.
# This requires knowing the K8s worker node security group ID or pod CIDR.
# For now, allowing from the entire VPC as a placeholder. This should be refined.

keycloak_database_security_group = ec2.SecurityGroup(
    f"keycloak-database-security-group-{env_name}",
    name=f"keycloak-database-{target_vpc_name}-{env_name}",
    description="Access control for the keycloak database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[target_vpc["cidr"]],  # Placeholder: Allow from VPC.
            # Refine this.
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description=(
                f"Access to Postgres from K8s cluster on {DEFAULT_POSTGRES_PORT}"
            ),
        ),
    ],
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

# --- Database (RDS instance largely unchanged) ---
rds_defaults = defaults(stack_info)["rds"]
rds_password = keycloak_config.require("rds_password")

keycloak_db_config = OLPostgresDBConfig(
    instance_name=f"keycloak-{stack_info.env_suffix}",
    password=rds_password,
    storage=keycloak_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[keycloak_database_security_group.id],  # Use the modified SG
    engine_major_version="16",
    db_name="keycloak",
    tags=aws_config.tags,
    **rds_defaults,
)
keycloak_db = OLAmazonDB(keycloak_db_config)

db_address = keycloak_db.db_instance.address
db_port = keycloak_db.db_instance.port

# Vault Database Backend (if still used for other consumers,
# otherwise can be removed if only for Keycloak EC2)
keycloak_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=keycloak_db_config.db_name,
    mount_point=f"{keycloak_db_config.engine}-keycloak",
    db_admin_username=keycloak_db_config.username,
    db_admin_password=rds_password,
    db_host=db_address,
)
keycloak_db_vault_backend = OLVaultDatabaseBackend(keycloak_db_vault_backend_config)


# --- Kubernetes Secrets for Keycloak ---
db_creds_secret_name = f"keycloak-db-credentials-{env_name}"
keycloak_db_k8s_secret = k8s.core.v1.Secret(
    f"keycloak-db-k8s-secret-{env_name}",
    metadata={
        "name": db_creds_secret_name,
        "namespace": keycloak_namespace_name,
    },
    string_data={
        "username": keycloak_db_config.username,
        "password": rds_password,
    },
    opts=ResourceOptions(provider=k8s_provider, depends_on=[keycloak_namespace]),
)

# Initial admin user secret for Keycloak Operator
# The operator will create one if not specified, but we can provide one.
# Name format for operator: <Keycloak CR name>-initial-admin
# Let's name our Keycloak CR "keycloak-instance"
keycloak_cr_name = f"keycloak-instance-{env_name}"
initial_admin_secret_name = f"{keycloak_cr_name}-initial-admin"

if "initial_admin_user" in secrets and "initial_admin_password" in secrets:
    keycloak_initial_admin_k8s_secret = k8s.core.v1.Secret(
        f"keycloak-initial-admin-k8s-secret-{env_name}",
        metadata={
            "name": initial_admin_secret_name,
            "namespace": keycloak_namespace_name,
        },
        string_data={
            "username": secrets["initial_admin_user"],
            "password": secrets["initial_admin_password"],
        },
        opts=ResourceOptions(provider=k8s_provider, depends_on=[keycloak_namespace]),
    )


# --- Kubernetes ConfigMap for cache-ispn-jdbc-ping.xml ---
cache_config_file_path = Path(__file__).parent.parent.parent.joinpath(
    "bilder", "images", "keycloak", "files", "cache-ispn-jdbc-ping.xml"
)
cache_config_content = cache_config_file_path.read_text()

keycloak_cache_configmap_name = f"keycloak-cache-config-{env_name}"
keycloak_cache_configmap = k8s.core.v1.ConfigMap(
    f"keycloak-cache-configmap-resource-{env_name}",
    metadata={
        "name": keycloak_cache_configmap_name,
        "namespace": keycloak_namespace_name,
    },
    data={"cache-ispn-jdbc-ping.xml": cache_config_content},
    opts=ResourceOptions(provider=k8s_provider, depends_on=[keycloak_namespace]),
)


# --- Remove EC2 resources provisioning ---


# --- ACM Certificate (Still needed for Ingress/Gateway TLS) ---
# This ACM certificate ARN might be used by Traefik depending on its configuration.
# The Gateway API `Gateway` resource typically expects a Kubernetes `Secret`
# of type `kubernetes.io/tls`.
# Provisioning such a Secret from an ACM certificate is not directly handled here.
keycloak_web_acm_cert = acm.Certificate(
    f"keycloak-gateway-acm-certificate-{env_name}",  # Renamed for clarity
    domain_name=keycloak_domain,
    validation_method="DNS",
    tags=aws_config.merged_tags({"Name": f"keycloak-gateway-{keycloak_domain}"}),
)

keycloak_acm_cert_validation_records = (
    keycloak_web_acm_cert.domain_validation_options.apply(
        partial(
            acm_certificate_validation_records,
            cert_name=f"keycloak-gateway-{env_name}",
            zone_id=mitol_zone_id,
            stack_info=stack_info,
        )
    )
)

keycloak_web_acm_validated_cert = acm.CertificateValidation(
    f"wait-for-keycloak-gateway-acm-cert-validation-{env_name}",
    certificate_arn=keycloak_web_acm_cert.arn,
    validation_record_fqdns=keycloak_acm_cert_validation_records.apply(
        lambda validation_records: [
            validation_record.fqdn for validation_record in validation_records
        ]
    ),
)


# --- Deploy Keycloak Operator ---
# Using official YAMLs from Keycloak documentation.
# Ensure the version is appropriate for your desired Keycloak version.
keycloak_operator_chart_version = (
    keycloak_config.get("operator_chart_version") or "26.2.4"
)  # Example version
keycloak_operator_crd_keycloaks_url = f"https://raw.githubusercontent.com/keycloak/keycloak-k8s-resources/{keycloak_operator_chart_version}/kubernetes/keycloaks.k8s.keycloak.org-v1.yml"
keycloak_operator_crd_imports_url = f"https://raw.githubusercontent.com/keycloak/keycloak-k8s-resources/{keycloak_operator_chart_version}/kubernetes/keycloakrealmimports.k8s.keycloak.org-v1.yml"
keycloak_operator_deployment_url = f"https://raw.githubusercontent.com/keycloak/keycloak-k8s-resources/{keycloak_operator_chart_version}/kubernetes/kubernetes.yml"

keycloak_operator_crd_keycloaks = ConfigFile(
    f"keycloak-operator-crd-keycloaks-{env_name}",
    file=keycloak_operator_crd_keycloaks_url,
    opts=ResourceOptions(provider=k8s_provider),
)
keycloak_operator_crd_imports = ConfigFile(
    f"keycloak-operator-crd-imports-{env_name}",
    file=keycloak_operator_crd_imports_url,
    opts=ResourceOptions(
        provider=k8s_provider, depends_on=[keycloak_operator_crd_keycloaks]
    ),
)

keycloak_operator_deployment = ConfigFile(
    f"keycloak-operator-deployment-{env_name}",
    file=keycloak_operator_deployment_url,
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[keycloak_operator_crd_imports, keycloak_namespace],
    ),
)


# --- Define Keycloak Custom Resource ---
keycloak_image_tag = keycloak_config.get("keycloak_image_tag") or secrets.get(
    "keycloak_version", "latest"
)
keycloak_image_repo = (
    "610119931565.dkr.ecr.us-east-1.amazonaws.com/dockerhub/mitodl/keycloak"
)

keycloak_kc_url = secrets.get(
    "KC_URL", f"https://{keycloak_domain}"
)  # From .env, ultimately from secrets
keycloak_kc_hostname = secrets.get("KC_HOSTNAME", keycloak_domain)

keycloak_cr_spec = {
    "image": f"{keycloak_image_repo}:{keycloak_image_tag}",
    "instances": keycloak_config.get_int("replicas") or 2,
    "db": {
        "vendor": "postgres",
        "host": db_address,
        "port": db_port.apply(int),
        "database": keycloak_db_config.db_name,
        "usernameSecret": {
            "name": db_creds_secret_name,
            "key": "username",
        },
        "passwordSecret": {
            "name": db_creds_secret_name,
            "key": "password",
        },
        "schema": secrets.get("KC_DB_SCHEMA", "public"),
    },
    "http": {
        "httpEnabled": True,
    },
    "hostname": {
        "hostname": keycloak_kc_hostname,
        # Default, can be true for prod. Was effectively false with --hostname=${KC_URL}
        "strict": False,
    },
    "additionalOptions": [
        {
            "name": "spi-sticky-session-encoder-infinispan-should-attach-route",
            "value": "false",
        },
        {"name": "spi-login-provider", "value": "ol-freemarker"},
        {"name": "proxy-headers", "value": "xforwarded"},
    ],
    "unsupported": {
        "podTemplate": {
            "spec": {
                "containers": [
                    {
                        "name": "keycloak",
                        "env": [
                            {
                                "name": "JGROUPS_DISCOVERY_EXTERNAL_IP",
                                "valueFrom": {
                                    "fieldRef": {"fieldPath": "status.podIP"}
                                },
                            },
                            {"name": "KC_DB_URL_HOST", "value": db_address.apply(str)},
                            {
                                "name": "KC_DB_URL_DATABASE",
                                "value": keycloak_db_config.db_name,
                            },
                            {
                                "name": "KC_DB_USERNAME",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": db_creds_secret_name,
                                        "key": "username",
                                    }
                                },
                            },
                            {
                                "name": "KC_DB_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": db_creds_secret_name,
                                        "key": "password",
                                    }
                                },
                            },
                            {
                                "name": "KC_DB_SCHEMA",
                                "value": secrets.get("KC_DB_SCHEMA", "public"),
                            },
                            # For cache-ispn-jdbc-ping.xml stack name
                            {"name": "KC_DB", "value": "postgres"},
                        ],
                        "volumeMounts": [
                            {
                                "name": "keycloak-cache-config-volume",
                                "mountPath": "/opt/keycloak/conf/cache-ispn-jdbc-ping.xml",  # noqa: E501
                                "subPath": "cache-ispn-jdbc-ping.xml",
                            }
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "keycloak-cache-config-volume",
                        "configMap": {"name": keycloak_cache_configmap_name},
                    }
                ],
            }
        }
    },
}

keycloak_custom_resource = k8s.apiextensions.CustomResource(
    f"keycloak-cr-{env_name}",
    api_version="k8s.keycloak.org/v2alpha1",
    kind="Keycloak",
    metadata={
        "name": keycloak_cr_name,
        "namespace": keycloak_namespace_name,
        "labels": {"app": "keycloak"},
    },
    spec=keycloak_cr_spec,
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[
            keycloak_operator_deployment,
            keycloak_db_k8s_secret,
            keycloak_initial_admin_k8s_secret
            if "initial_admin_user" in secrets
            else keycloak_namespace,
            keycloak_cache_configmap,
        ],
    ),
)

# --- Traefik Gateway API Resources ---
# Assumes a Traefik GatewayClass named 'traefik' is installed in the cluster.
gateway_class_name = "traefik"
keycloak_gateway_name = f"keycloak-gateway-{env_name}"
# This secret needs to be provisioned with the cert for keycloak_domain
keycloak_tls_secret_name = f"keycloak-tls-secret-{env_name}"

# Define the Gateway resource
keycloak_traefik_gateway = Gateway(
    f"keycloak-traefik-gateway-{env_name}",
    metadata={
        "name": keycloak_gateway_name,
        "namespace": keycloak_namespace_name,
    },
    spec={
        "gatewayClassName": gateway_class_name,
        "listeners": [
            {
                "name": "http",
                "port": 80,
                "protocol": "HTTP",
                "allowedRoutes": {"namespaces": {"from": "Same"}},
            },
            {
                "name": "https",
                "port": 443,
                "protocol": "HTTPS",
                "allowedRoutes": {"namespaces": {"from": "Same"}},
                "tls": {
                    "mode": "Terminate",
                    "certificateRefs": [
                        {
                            "kind": "Secret",
                            "group": "",  # Core group for Secret
                            "name": keycloak_tls_secret_name,
                        }
                    ],
                },
            },
        ],
    },
    opts=ResourceOptions(provider=k8s_provider, depends_on=[keycloak_namespace]),
)
# Note: The Secret 'keycloak_tls_secret_name' needs to be created and populated
# with a TLS certificate for 'keycloak_domain'. This can be done via
# cert-manager or manually.

# Define the HTTPRoute resource
# The Keycloak operator typically creates a service named after the Keycloak CR.
keycloak_operator_service_name = keycloak_cr_name
keycloak_operator_service_port = 8080  # Default HTTP port Keycloak listens on

keycloak_http_route = HTTPRoute(
    f"keycloak-httproute-{env_name}",
    metadata={
        "name": f"keycloak-httproute-{env_name}",
        "namespace": keycloak_namespace_name,
    },
    spec={
        "parentRefs": [
            {
                "name": keycloak_gateway_name,
                "namespace": keycloak_namespace_name,
            }
        ],
        "hostnames": [keycloak_domain],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "backendRefs": [
                    {
                        "name": keycloak_operator_service_name,
                        "port": keycloak_operator_service_port,
                        "kind": "Service",
                        "group": "",  # Core group for Service
                    }
                ],
            }
        ],
    },
    opts=ResourceOptions(
        provider=k8s_provider,
        depends_on=[keycloak_traefik_gateway, keycloak_custom_resource],
    ),
)


# --- Vault policy definition (remains, but auth method for pods would change) ---
keycloak_server_vault_policy = vault.Policy(
    "keycloak-server-vault-policy",
    name="keycloak-server",
    policy=Path(__file__).parent.joinpath("keycloak_server_policy.hcl").read_text(),
)

# If Keycloak pods need to authenticate with Vault, a Kubernetes auth method role
# would be configured here.

# Vault KV2 mount definition (remains if Vault is still source of some secrets)
keycloak_server_vault_mount = vault.Mount(
    "keycloak-server-configuration-secrets-mount",
    path="secret-keycloak",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration credentials and secrets used by keycloak",
    opts=ResourceOptions(delete_before_replace=True),
)

keycloak_server_secrets_in_vault = vault.generic.Secret(
    "keycloak-server-configuration-secrets-in-vault",
    path=keycloak_server_vault_mount.path.apply(lambda p: f"{p}/keycloak-secrets"),
    data_json=json.dumps(secrets),
)

# TODO MD 20230206 revisit this, probably need to export more things  # noqa: E501, FIX002, TD002, TD003, TD004
export(
    "keycloak_app",
    {
        "rds_host": db_address,
        "keycloak_domain": keycloak_domain,
        "keycloak_namespace": keycloak_namespace_name,
        "keycloak_cr_name": keycloak_cr_name,
    },
)
