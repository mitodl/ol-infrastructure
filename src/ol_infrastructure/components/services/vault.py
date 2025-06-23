"""
This module controls mounting and configuration of secret backends in Vault.

This includes:
- Mount a backend at the configured mount point
- Set the configuration according to the requirements of the backend type
- Control the lease TTL settings
- Define the base set of roles according to our established best practices
"""

import json
import textwrap
from enum import Enum
from string import Template
from typing import Any, Literal

import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_aws.acmpca import Certificate, CertificateValidityArgs
from pulumi_vault import Mount, aws, database, generic, pkisecret
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from bridge.lib.magic_numbers import (
    DEFAULT_MONGODB_PORT,
    DEFAULT_MYSQL_PORT,
    DEFAULT_POSTGRES_PORT,
    ONE_MONTH_SECONDS,
)
from ol_infrastructure.lib.vault import (
    VaultPKIKeyTypeBits,
    mongodb_role_statements,
    mysql_role_statements,
    postgres_role_statements,
)

SIX_MONTHS = 60 * 60 * 24 * 30 * 6
TWELVE_MONTHS = 60 * 60 * 24 * 30 * 12
VAULT_API_URL = "https://active.vault.service.consul/v1"

CERTIFICATE_CONFIG = {
    "country": "US",
    "state": "Massachusetts",
    "city": "Cambridge",
    "organization": "MIT",
    "organizational_unit": "Open Learning",
    "zip_code": "02139",
}


class DBEngines(str, Enum):
    """Constraints for valid engine types that are supported by this component."""

    postgres = "postgresql"
    mariadb = "mysql"
    mysql = "mysql"  # noqa: PIE796
    mysql_rds = "mysql_rds"
    mongodb = "mongodb"
    mongodb_atlas = "mongodbatlas"


class OLVaultDatabaseConfig(BaseModel):
    """Configuration object for Vault database backend resource."""

    db_name: str
    db_port: int
    db_connection: str
    mount_point: str
    db_admin_username: str
    db_admin_password: str
    verify_connection: bool = True
    db_host: str | Output[str]
    max_ttl: int = ONE_MONTH_SECONDS * 6
    default_ttl: int = ONE_MONTH_SECONDS * 3
    connection_options: dict[str, str] | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLVaultPostgresDatabaseConfig(OLVaultDatabaseConfig):
    """Configuration object for Postgres database to register with Vault."""

    db_port: int = DEFAULT_POSTGRES_PORT
    # The db_connection strings are passed through the `.format` method so the variables
    # that need to remain in the template to be passed to Vault are wrapped in 4 pairs
    # of braces. TMM 2020-09-01
    db_connection: str = (
        "postgresql://{{{{username}}}}:{{{{password}}}}@{db_host}:{db_port}/{db_name}"
    )
    db_type: str = DBEngines.postgres.value
    role_statements: dict[str, dict[str, list[Template]]] = postgres_role_statements


class OLVaultMysqlDatabaseConfig(OLVaultDatabaseConfig):
    """Configuration object for MySQL/MariaDB database to register with Vault."""

    db_port: int = DEFAULT_MYSQL_PORT
    db_connection: str = "{{{{username}}}}:{{{{password}}}}@tcp({db_host}:{db_port})/"
    db_type: str = DBEngines.mysql_rds.value
    role_statements: dict[str, dict[str, list[Template]]] = mysql_role_statements


class OLVaultMongoDatabaseConfig(OLVaultDatabaseConfig):
    """Configuration object for MongoDB instances to register with Vault."""

    db_port: int = DEFAULT_MONGODB_PORT
    db_connection: str = (
        "mongodb://{{{{username}}}}:{{{{password}}}}@{db_host}:{db_port}/admin"
    )
    db_type: str = DBEngines.mongodb.value
    role_statements: dict[str, dict[str, list[Template]]] = mongodb_role_statements


class OLVaultDatabaseBackend(ComponentResource):
    """Resource for encapsulating the steps needed to connect Vault to a database."""

    def __init__(
        self,
        db_config: OLVaultMysqlDatabaseConfig | OLVaultPostgresDatabaseConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            f"ol:services:Vault:DatabaseBackend:{db_config.db_type}",
            db_config.db_name,
            None,
            opts,
        )

        resource_opts = ResourceOptions(parent=self, delete_before_replace=True)

        self.db_mount = Mount(
            f"{db_config.db_name}-mount-point",
            opts=resource_opts,
            path=db_config.mount_point,
            type="database",
            max_lease_ttl_seconds=db_config.max_ttl,
            default_lease_ttl_seconds=db_config.default_ttl,
        )

        db_option_dict = {}
        credentials_dict = {
            "username": db_config.db_admin_username,
            "password": db_config.db_admin_password,
        }

        if hasattr(db_config, "db_connection"):
            db_option_dict.update(
                {
                    "connection_url": self.format_connection_string(db_config),
                    **credentials_dict,
                }
            )

        db_option_dict.update(db_config.connection_options or {})
        self.db_connection = database.SecretBackendConnection(
            f"{db_config.db_name}-database-connection",
            opts=resource_opts.merge(
                ResourceOptions(parent=self.db_mount, depends_on=self.db_mount)
            ),
            backend=self.db_mount.path,
            verify_connection=db_config.verify_connection,
            allowed_roles=sorted(db_config.role_statements.keys()),
            name=db_config.db_name,
            data=credentials_dict,
            **{db_config.db_type: db_option_dict},
        )

        self.db_roles = {}
        for role_name, role_defs in db_config.role_statements.items():
            self.db_roles[role_name] = database.SecretBackendRole(
                f"{db_config.db_name}-database-role-{role_name}",
                opts=resource_opts.merge(ResourceOptions(parent=self.db_connection)),
                name=role_name,
                backend=self.db_mount.path,
                db_name=db_config.db_name,
                creation_statements=[
                    textwrap.dedent(statement).strip()
                    for statement in [
                        statement_template.substitute(app_name=db_config.db_name)
                        for statement_template in role_defs["create"]
                    ]
                ],
                revocation_statements=[
                    textwrap.dedent(statement).strip()
                    for statement in [
                        statement_template.substitute(app_name=db_config.db_name)
                        for statement_template in role_defs["revoke"]
                    ]
                ],
                renew_statements=[
                    textwrap.dedent(statement).strip()
                    for statement in [
                        statement_template.substitute(app_name=db_config.db_name)
                        for statement_template in role_defs["renew"]
                    ]
                ],
                rollback_statements=[
                    textwrap.dedent(statement).strip()
                    for statement in [
                        statement_template.substitute(app_name=db_config.db_name)
                        for statement_template in role_defs["rollback"]
                    ]
                ],
                max_ttl=db_config.max_ttl,
                default_ttl=db_config.default_ttl,
            )
        self.register_outputs({})

    def format_connection_string(
        self, db_config: OLVaultDatabaseConfig
    ) -> Output[str] | str:
        if isinstance(db_config.db_host, Output):
            connection_url: str | Output[str] = db_config.db_host.apply(
                lambda host: db_config.db_connection.format_map(
                    {
                        "db_port": db_config.db_port,
                        "db_name": db_config.db_name,
                        "db_host": host,
                    }
                )
            )
        else:
            connection_url = db_config.db_connection.format_map(
                {
                    "db_port": db_config.db_port,
                    "db_name": db_config.db_name,
                    "db_host": db_config.db_host,
                }
            )
        return connection_url


class OLVaultAWSSecretsEngineConfig(BaseModel):
    app_name: str
    aws_access_key: str
    default_lease_ttl_seconds: int = ONE_MONTH_SECONDS * 6
    max_lease_ttl_seconds: int = ONE_MONTH_SECONDS * 3
    description: str
    aws_secret_key: str
    vault_backend_path: str
    policy_documents: dict[str, str]
    credential_type: str = "iam_user"
    iam_tags: list[str] = ["OU=operations", "vault_managed=True"]

    @field_validator("vault_backend_path")
    @classmethod
    def is_valid_path(cls, vault_backend_path: str) -> str:
        if vault_backend_path.startswith("/") or vault_backend_path.endswith("/"):
            msg = f"The specified path value {vault_backend_path} can not start or end with a slash"  # noqa: E501
            raise ValueError(msg)
        return vault_backend_path


class OLVaultAWSSecretsEngine(ComponentResource):
    def __init__(
        self,
        engine_config: OLVaultAWSSecretsEngineConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:services:Vault:AWSSecretsEngine", engine_config.app_name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

        self.aws_secrets_engine = aws.SecretBackend(
            # TODO verify app_name exists based on Apps class in ol_types  # noqa: E501, FIX002, TD002, TD004
            f"aws-{engine_config.app_name}",
            access_key=engine_config.aws_access_key,
            secret_key=engine_config.aws_secret_key,
            path=f"aws-{engine_config.vault_backend_path}",
            opts=resource_options,
        )

        for role_name, policy in engine_config.policy_documents.items():
            aws.SecretBackendRole(
                role_name,
                backend=self.aws_secrets_engine.name,
                credential_type=engine_config.credential_type,
                iam_tags=engine_config.iam_tags,
                name=role_name,
                policy_document=json.dumps(policy),
                opts=resource_options,
            )

        self.register_outputs({})


class OLVaultPKIIntermediateCABackendConfig(BaseModel):
    max_ttl: int = ONE_MONTH_SECONDS * 12
    default_ttl: int = ONE_MONTH_SECONDS * 12
    acmpca_rootca_arn: Output = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLVaultPKIIntermediateCABackend(ComponentResource):
    """
    PKI Intermediate CA Backend is set to pki-intermediate-ca.

    This should be used to create and configure the intermediate vault pki CA backend
    which is used to sign certificates used by the intermediate PKI's in the different
    environments.

    AWS Private CA --- pki-intermediate-ca --- sign -----> pki-intermediate-mitx-qa
                                                       pki-intermediate-mitxpro-qa
                                                       pki-intermeidate-...
    """

    def __init__(
        self,
        backend_config: OLVaultPKIIntermediateCABackendConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:services:Vault:PKI:IntermediateCABackend",
            "pki-intermediate-ca",
            None,
            opts,
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

        # Create the pki-intermediate-ca endpoint / mount
        self.pki_intermediate_ca_backend = Mount(
            "pki-intermediate-ca",
            opts=resource_options,
            path="pki-intermediate-ca",
            type="pki",
            description="Backend to create certs for pki-intermediate-{env} backends",
            max_lease_ttl_seconds=backend_config.max_ttl,
            default_lease_ttl_seconds=backend_config.default_ttl,
        )

        # Generate CSR for pki-intermediate-ca backend
        self.pki_intermediate_ca_cert_request = (
            pkisecret.SecretBackendIntermediateCertRequest(
                "pki-intermediate-ca-csr",
                backend=self.pki_intermediate_ca_backend.id,
                common_name="pki-intermediate-ca Intermediate Authority",
                type="internal",
                country=CERTIFICATE_CONFIG["country"],
                province=CERTIFICATE_CONFIG["state"],
                locality=CERTIFICATE_CONFIG["city"],
                organization=CERTIFICATE_CONFIG["organization"],
                ou=CERTIFICATE_CONFIG["organizational_unit"],
                postal_code=CERTIFICATE_CONFIG["zip_code"],
                opts=ResourceOptions(parent=self.pki_intermediate_ca_backend),
            )
        )

        # Create a default issuer role for pki-intermediate-ca
        self.pki_intermediate_ca_default_issuer_config = OLVaultPKIIntermediateRoleConfig(  # noqa: E501
            pki_intermediate_backend_mount_path=self.pki_intermediate_ca_backend.path,
            role_name="default-issuer",
            key_config=VaultPKIKeyTypeBits.rsa,
            allowed_domains=["mit.edu"],
            cert_type="server",
            resource_name="pki-intermediate-ca-role-default-issuer",
        )
        self.pki_intermediate_ca_default_issuer = OLVaultPKIIntermediateRole(
            self.pki_intermediate_ca_default_issuer_config,
            opts=ResourceOptions(parent=self.pki_intermediate_ca_cert_request),
        )

        # Sign passed CSR for pki-intermediate-ca from the AWS Private CA
        self.pki_intermediate_ca_aws_signed_csr = Certificate(
            "pki-intermediate-ca-aws-signed-csr",
            certificate_authority_arn=backend_config.acmpca_rootca_arn,
            certificate_signing_request=self.pki_intermediate_ca_cert_request.csr,
            signing_algorithm="SHA256WITHRSA",
            validity=CertificateValidityArgs(type="YEARS", value="7"),
            template_arn=(
                "arn:aws:acm-pca:::template/SubordinateCACertificate_PathLen1/V1"
            ),
            opts=ResourceOptions(parent=self.pki_intermediate_ca_default_issuer),
        )

        # Associate the ACMPCA Root CA cert with pki-intermediate-ca
        self.pki_intermediate_ca_backend_config = pkisecret.SecretBackendConfigCa(
            "pki-intermediate-ca-backend-config",
            backend=self.pki_intermediate_ca_backend.id,
            pem_bundle=self.pki_intermediate_ca_aws_signed_csr.certificate_chain,
            opts=ResourceOptions(parent=self.pki_intermediate_ca_aws_signed_csr),
        )

        # Install the signed pki-intermediate-ca from AWS as pki-intermediate-ca
        self.pki_intermediate_ca_set_signed = (
            pkisecret.SecretBackendIntermediateSetSigned(
                "pki-intermediate-ca-set-signed",
                backend=self.pki_intermediate_ca_backend.id,
                certificate=self.pki_intermediate_ca_aws_signed_csr.certificate,
                opts=ResourceOptions(parent=self.pki_intermediate_ca_aws_signed_csr),
            )
        )

        # Configure the CRL and issuing endpoints
        self.pki_intermediate_ca_config_urls = pkisecret.SecretBackendConfigUrls(
            "pki-intermediate-ca-config-url",
            backend=self.pki_intermediate_ca_backend.id,
            crl_distribution_points=[f"{VAULT_API_URL}/pki_intermediate_ca/crl"],
            issuing_certificates=[f"{VAULT_API_URL}/pki_intermediate_ca/ca"],
        )
        self.register_outputs(
            {"pki_intermediate_ca": self.pki_intermediate_ca_backend.id}
        )


class OLVaultPKIIntermediateEnvBackendConfig(BaseModel):
    environment_name: str  # e.g. mitx-qa
    max_ttl: int = ONE_MONTH_SECONDS * 12
    default_ttl: int = ONE_MONTH_SECONDS * 12
    acmpca_rootca_arn: Output = None
    parent_intermediate_ca: OLVaultPKIIntermediateCABackend = None  # type: ignore  # noqa: PGH003
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLVaultPKIIntermediateEnvBackend(ComponentResource):
    """
    Create PKI Intermediate Backends per environment.

    This should be used to create and configure an intermediate vault pki backend
    in the specified environment. The certificate for this backend will be signed
    by the pki-intermediate-ca which in turn is signed by our offline CA.
    """

    def __init__(
        self,
        backend_config: OLVaultPKIIntermediateEnvBackendConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:services:Vault:PKI:IntermediateEnvBackendConfig",
            f"pki-intermediate-{backend_config.environment_name}",
            None,
            opts,
        )

        resource_options = ResourceOptions(
            parent=self,
            depends_on=[
                backend_config.parent_intermediate_ca.pki_intermediate_ca_default_issuer
            ],
            delete_before_replace=True,
        ).merge(opts)

        # Create the pki-intermediate-{env} endpoint / mount
        self.pki_intermediate_environment_backend = Mount(
            f"pki-intermediate-{backend_config.environment_name}",
            path=f"pki-intermediate-{backend_config.environment_name}",
            type="pki",
            description=(
                "Backend to create certs for "
                f"pki-intermediate-{backend_config.environment_name} backends"
            ),
            max_lease_ttl_seconds=backend_config.max_ttl,
            default_lease_ttl_seconds=backend_config.default_ttl,
            opts=resource_options,
        )

        # Generate CSR for pki-intermediate-{env} backend
        self.pki_intermediate_environment_cert_request = (
            pkisecret.SecretBackendIntermediateCertRequest(
                f"pki-intermediate-{backend_config.environment_name}-csr",
                backend=self.pki_intermediate_environment_backend.id,
                common_name=(
                    f"pki-intermediate-{backend_config.environment_name} "
                    "Intermediate Authority"
                ),
                type="internal",
                country=CERTIFICATE_CONFIG["country"],
                province=CERTIFICATE_CONFIG["state"],
                locality=CERTIFICATE_CONFIG["city"],
                organization=CERTIFICATE_CONFIG["organization"],
                ou=CERTIFICATE_CONFIG["organizational_unit"],
                postal_code=CERTIFICATE_CONFIG["zip_code"],
                opts=ResourceOptions(parent=self.pki_intermediate_environment_backend),
            )
        )

        # Create a default issuer role for pki-intermediate-{env}
        self.pki_intermediate_environment_default_issuer_config = OLVaultPKIIntermediateRoleConfig(  # noqa: E501
            pki_intermediate_backend_mount_path=self.pki_intermediate_environment_backend.path,
            role_name="default-issuer",
            key_config=VaultPKIKeyTypeBits.rsa,
            allowed_domains=["mit.edu"],
            cert_type="server",
            resource_name=(
                f"pki-intermediate-{backend_config.environment_name}-default-issuer"
            ),
        )
        self.pki_intermediate_environment_default_issuer = OLVaultPKIIntermediateRole(
            self.pki_intermediate_environment_default_issuer_config,
            opts=ResourceOptions(parent=self.pki_intermediate_environment_cert_request),
        )

        # Sign passed CSR for pki-intermediate-{env} with pki-intermediate-ca
        self.pki_intermediate_environment_signed_csr = (
            pkisecret.SecretBackendRootSignIntermediate(
                f"pki-intermediate-{backend_config.environment_name}-signed-csr",
                backend="pki-intermediate-ca",
                common_name=(
                    f"pki-intermediate-{backend_config.environment_name} "
                    "Intermediate Authority"
                ),
                csr=self.pki_intermediate_environment_cert_request.csr,
                opts=ResourceOptions(
                    parent=self.pki_intermediate_environment_default_issuer
                ),
            )
        )

        # Associate pki-intermediate-ca bundle with this new pki-intermediate-{env}
        self.pki_intermediate_environment_backend_config = pkisecret.SecretBackendConfigCa(  # noqa: E501
            f"pki-intermediate-{backend_config.environment_name}-backend-config",
            backend=self.pki_intermediate_environment_backend.id,
            pem_bundle=self.pki_intermediate_environment_signed_csr.certificate_bundle,
            opts=ResourceOptions(parent=self.pki_intermediate_environment_signed_csr),
        )

        # Install the signed pki-intermediate-ca from AWS as pki-intermediate-ca
        self.pki_intermediate_environment_set_signed = (
            pkisecret.SecretBackendIntermediateSetSigned(
                f"pki-intermediate-{backend_config.environment_name}-signed-cert",
                backend=self.pki_intermediate_environment_backend.id,
                certificate=self.pki_intermediate_environment_signed_csr.certificate,
                opts=ResourceOptions(
                    parent=self.pki_intermediate_environment_signed_csr
                ),
            )
        )

        # Install the signed pki-intermediate-ca from AWS as pki-intermediate-ca
        self.pki_intermediate_environment_config_urls = pkisecret.SecretBackendConfigUrls(  # noqa: E501
            f"pki-intermediate-{backend_config.environment_name}-config-url",
            backend=self.pki_intermediate_environment_backend.id,
            crl_distribution_points=[
                f"{VAULT_API_URL}/pki_intermediate_{backend_config.environment_name}/crl"
            ],
            issuing_certificates=[
                f"{VAULT_API_URL}/pki_intermediate_{backend_config.environment_name}/ca"
            ],
            opts=ResourceOptions(
                depends_on=[
                    self.pki_intermediate_environment_set_signed,
                    self.pki_intermediate_environment_backend_config,
                ],
                parent=self.pki_intermediate_environment_signed_csr,
            ),
        )

        self.register_outputs({})


class OLVaultPKIIntermediateRoleConfig(BaseModel):
    pki_intermediate_backend_mount_path: Output = None
    role_name: str
    key_config: VaultPKIKeyTypeBits
    max_ttl: int = ONE_MONTH_SECONDS * 7
    default_ttl: int = ONE_MONTH_SECONDS * 6
    key_usages: list[str] = ["DigitalSignature", "KeyAgreement", "KeyEncipherment"]
    allowed_domains: list[str]
    allow_subdomains: bool = False
    cert_type: str  # Should be client or server
    resource_name: str
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("cert_type")
    @classmethod
    def is_valid_cert_type(cls, cert_type: str) -> str:
        if cert_type not in {"server", "client"}:
            msg = f"The specified certificate type {cert_type} has to be either client or server"  # noqa: E501
            raise ValueError(msg)
        return cert_type


class OLVaultPKIIntermediateRole(ComponentResource):
    def __init__(
        self,
        role_config: OLVaultPKIIntermediateRoleConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:services:Vault:PKI:IntermediateRoleConfig",
            role_config.resource_name,
            None,
            opts,
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

        # Default is True for both flags
        flag_type = {
            "client": {"client_flag": True, "server_flag": False},
            "server": {"server_flag": True, "client_flag": False},
        }

        self.pki_intermediate_env_client_role = pkisecret.SecretBackendRole(
            role_config.resource_name,
            # forcing role name so that pulumi doesn't add suffix
            name=role_config.role_name,
            backend=role_config.pki_intermediate_backend_mount_path,
            allowed_domains=role_config.allowed_domains,
            allow_glob_domains=True,
            allow_subdomains=role_config.allow_subdomains,
            key_type=role_config.key_config.name,
            key_bits=role_config.key_config.value,
            ttl=role_config.default_ttl,
            max_ttl=role_config.max_ttl,
            key_usages=role_config.key_usages,
            generate_lease=True,
            countries=[CERTIFICATE_CONFIG["country"]],
            provinces=[CERTIFICATE_CONFIG["state"]],
            localities=[CERTIFICATE_CONFIG["city"]],
            organizations=[CERTIFICATE_CONFIG["organization"]],
            organization_unit=[CERTIFICATE_CONFIG["organizational_unit"]],
            postal_codes=[CERTIFICATE_CONFIG["zip_code"]],
            opts=resource_options,
            **flag_type[role_config.cert_type],
        )

        self.register_outputs({})


# TODO: @Ardiea expand to include support for transformationRefs  # noqa: FIX002, TD002
# https://kubernetes.io/docs/concepts/configuration/secret/#secret-types
class OLVaultK8SSecretConfig(BaseModel):
    annotations: dict[str, str] | None = None
    dest_secret_annotations: dict[str, str] | None = None
    dest_secret_create: bool = True
    dest_secret_labels: dict[str, str] | None = None
    dest_secret_name: str
    dest_secret_overwrite: bool = True
    # Ref: https://kubernetes.io/docs/concepts/configuration/secret/#secret-types
    dest_secret_type: Literal[
        "Opaque",
        "kubernetes.io/tls",
        "kubernetes.io/ssh-auth",
        "kubernetes.io/basic-auth",
    ] = "Opaque"  # noqa: S105
    exclude_raw: bool | None = True
    excludes: list[str] | None = [".*"]
    includes: list[str] | None = []
    kind: str
    labels: dict[str, str] | None = None
    mount: str | Output[str]
    mount_type: Literal["kv-v1", "kv-v2"] | None = None
    name: str
    refresh_after: str | None = None
    # TODO: @Ardiea Add support for multiple restart targets  # noqa: FIX002, TD002
    restart_target_kind: Literal["Deployment", "DaemonSet", "StatefulSet"] | None = None
    restart_target_name: str | None = None
    namespace: str
    path: str
    templates: dict[str, str | Output[str]] | None = None
    vaultauth: str
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="after")
    def restart_target_is_set(self):
        if not all((self.restart_target_kind, self.restart_target_name)) and any(
            (self.restart_target_kind, self.restart_target_name)
        ):
            msg = "Both restart_target_kind and restart_target_name must be set."
            raise ValueError(msg)
        return self


class OLVaultK8SStaticSecretConfig(OLVaultK8SSecretConfig):
    kind: str = "VaultStaticSecret"
    refresh_after: str | None = "1h"
    mount_type: Literal["kv-v1", "kv-v2"] = "kv-v2"
    contents: dict[str, Any] | None = None

    @field_validator("kind")
    @classmethod
    def is_valid_kind(cls, kind: str) -> str:
        if kind != "VaultStaticSecret":
            msg = "The only valid 'kind' for OLVaultK8SStaticSecret is 'VaultStaticSecret'"  # noqa: E501
            raise ValueError(msg)
        return kind


class OLVaultK8SDynamicSecretConfig(OLVaultK8SSecretConfig):
    kind: str = "VaultDynamicSecret"

    @field_validator("kind")
    @classmethod
    def is_valid_kind(cls, kind: str) -> str:
        if kind != "VaultDynamicSecret":
            msg = "The only valid 'kind' for OLVaultK8SDynamicSecret is 'VaultDynamicSecret'"  # noqa: E501
            raise ValueError(msg)
        return kind


class OLVaultK8SSecret(ComponentResource):
    def __init__(
        self,
        name: str,
        resource_config: OLVaultK8SSecretConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            f"ol:services:Vault:K8S:{resource_config.kind}",
            name,
            None,
            opts,
        )

        resource_opts = ResourceOptions.merge(ResourceOptions(parent=self), opts)

        secret_def: dict[str, Any] = {
            "apiVersion": "secrets.hashicorp.com/v1beta1",
            "kind": resource_config.kind,
            "metadata": {
                "name": resource_config.name,
                "namespace": resource_config.namespace,
                "labels": resource_config.labels,
                "annotations": resource_config.annotations,
            },
            "spec": {
                "mount": resource_config.mount,
                "path": resource_config.path,
                "destination": {
                    "name": resource_config.dest_secret_name,
                    "type": resource_config.dest_secret_type,
                    "overwrite": resource_config.dest_secret_overwrite,
                    "create": resource_config.dest_secret_create,
                    "labels": resource_config.dest_secret_labels,
                    "annotations": resource_config.dest_secret_annotations,
                },
                "vaultAuthRef": resource_config.vaultauth,
            },
        }

        if resource_config.restart_target_kind and resource_config.restart_target_name:
            secret_def["spec"]["rolloutRestartTargets"] = [
                {
                    "name": resource_config.restart_target_name,
                    "kind": resource_config.restart_target_kind,
                },
            ]

        if resource_config.templates:
            transformation_block: dict[str, Any] = {
                "excludeRaw": resource_config.exclude_raw,
                "excludes": resource_config.excludes,
                "includes": resource_config.includes,
                "templates": {},
            }
            for name, text in resource_config.templates.items():  # noqa: PLR1704
                transformation_block["templates"][name] = {"text": str(text)}
            secret_def["spec"]["destination"]["transformation"] = transformation_block

        if isinstance(resource_config, OLVaultK8SStaticSecretConfig):
            secret_def["spec"]["type"] = str(resource_config.mount_type)
            secret_def["spec"]["refreshAfter"] = resource_config.refresh_after
            if resource_config.contents:
                generic.Secret(
                    f"{resource_config.name}-vault-static-secret",
                    opts=resource_opts,
                    data_json=json.dumps(resource_config.contents),
                    path=Output.from_input(resource_config.mount).apply(
                        lambda mount: f"{mount}/{resource_config.path}"
                    ),
                )

        self.vault_secret_resource = kubernetes.yaml.v2.ConfigGroup(
            f"OLVaultK8SSecret-{resource_config.namespace}-{resource_config.name}",
            objs=[secret_def],
            opts=resource_opts,
        )


class OLVaultK8SResourcesConfig(BaseModel):
    annotations: dict[str, str] | None = None
    application_name: str
    labels: dict[str, str] | None = None
    namespace: str
    vault_address: str
    vault_auth_endpoint: str | Output[str]
    vault_auth_role_name: str | Output[str]
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLVaultK8SResources(ComponentResource):
    """Resource for encapsulating the components required to create a
    vault-k8s integration
    """

    def __init__(
        self,
        resource_config: OLVaultK8SResourcesConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:services:Vault:K8S:ResourcesConfig",
            resource_config.application_name,
            None,
            opts,
        )
        resource_opts = ResourceOptions.merge(ResourceOptions(parent=self), opts)

        self.service_account_name = f"{resource_config.application_name}-vault"
        self.connection_name = f"{resource_config.application_name}-vault-connection"
        self.auth_name = f"{resource_config.application_name}-auth"

        self.service_account = kubernetes.core.v1.ServiceAccount(
            f"{resource_config.application_name}-vault-service-account",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=self.service_account_name,
                namespace=resource_config.namespace,
                labels=resource_config.labels,
                annotations=resource_config.annotations,
            ),
            automount_service_account_token=False,
            opts=resource_opts,
        )

        self.cluster_role_binding = kubernetes.rbac.v1.ClusterRoleBinding(
            f"{resource_config.application_name}-vault-cluster-role-binding",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=f"{self.service_account_name}:cluster-auth",
                namespace=resource_config.namespace,
                labels=resource_config.labels,
                annotations=resource_config.annotations,
            ),
            role_ref=kubernetes.rbac.v1.RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="ClusterRole",
                name="system:auth-delegator",
            ),
            subjects=[
                kubernetes.rbac.v1.SubjectArgs(
                    kind="ServiceAccount",
                    name=self.service_account_name,
                    namespace=resource_config.namespace,
                ),
            ],
            opts=resource_opts,
        )

        self.vso_resources = kubernetes.yaml.v2.ConfigGroup(
            f"{resource_config.application_name}-vso-resources",
            objs=[
                {
                    "apiVersion": "secrets.hashicorp.com/v1beta1",
                    "kind": "VaultConnection",
                    "metadata": {
                        "name": self.connection_name,
                        "namespace": resource_config.namespace,
                        "labels": resource_config.labels,
                        "annotations": resource_config.annotations,
                    },
                    "spec": {
                        "address": resource_config.vault_address,
                        "skipTLSVerify": False,
                    },
                },
                {
                    "apiVersion": "secrets.hashicorp.com/v1beta1",
                    "kind": "VaultAuth",
                    "metadata": {
                        "name": self.auth_name,
                        "namespace": resource_config.namespace,
                        "labels": resource_config.labels,
                        "annotations": resource_config.annotations,
                    },
                    "spec": {
                        "method": "kubernetes",
                        "mount": resource_config.vault_auth_endpoint,
                        "vaultConnectionRef": self.connection_name,
                        "kubernetes": {
                            "role": resource_config.vault_auth_role_name,
                            "serviceAccount": self.service_account_name,
                        },
                    },
                },
            ],
            opts=resource_opts,
        )
