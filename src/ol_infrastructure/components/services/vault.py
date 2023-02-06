"""
This module controls mounting and configuration of secret backends in Vault.

This includes:
- Mount a backend at the configured mount point
- Set the configuration according to the requirements of the backend type
- Control the lease TTL settings
- Define the base set of roles according to our established best practices
"""
import json
from enum import Enum
from string import Template
from typing import Optional, Union

from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_vault import Mount, aws, database, pkisecret
from pydantic import BaseModel, validator

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
    db_host: Union[str, Output[str]]
    max_ttl: int = ONE_MONTH_SECONDS * 6
    default_ttl: int = ONE_MONTH_SECONDS * 6
    connection_options: Optional[dict[str, str]]

    class Config:
        arbitrary_types_allowed = True


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
    role_statements: dict[str, dict[str, Template]] = postgres_role_statements


class OLVaultMysqlDatabaseConfig(OLVaultDatabaseConfig):
    """Configuration object for MySQL/MariaDB database to register with Vault."""

    db_port: int = DEFAULT_MYSQL_PORT
    db_connection: str = "{{{{username}}}}:{{{{password}}}}@tcp({db_host}:{db_port})/"
    db_type: str = DBEngines.mysql_rds.value
    role_statements: dict[str, dict[str, Template]] = mysql_role_statements


class OLVaultMongoDatabaseConfig(OLVaultDatabaseConfig):
    """Configuration object for MongoDB instances to register with Vault."""

    db_port: int = DEFAULT_MONGODB_PORT
    db_connection: str = (
        "mongodb://{{{{username}}}}:{{{{password}}}}@{db_host}:{db_port}/admin"
    )
    db_type: str = DBEngines.mongodb.value
    role_statements: dict[str, dict[str, Template]] = mongodb_role_statements


class OLVaultDatabaseBackend(ComponentResource):
    """Resource for encapsulating the steps needed to connect Vault to a database."""

    def __init__(
        self,
        db_config: Union[OLVaultMysqlDatabaseConfig, OLVaultPostgresDatabaseConfig],
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            f"ol:services:Vault:DatabaseBackend:{db_config.db_type}",
            db_config.db_name,
            None,
            opts,
        )

        resource_opts = ResourceOptions.merge(
            ResourceOptions(parent=self), opts
        )  # type: ignore

        self.db_mount = Mount(
            f"{db_config.db_name}-mount-point",
            opts=resource_opts.merge(ResourceOptions(delete_before_replace=True)),
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
                ResourceOptions(parent=self.db_mount)
            ),  # type: ignore
            backend=db_config.mount_point,
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
                opts=resource_opts.merge(
                    ResourceOptions(parent=self.db_connection)
                ),  # type: ignore
                name=role_name,
                backend=self.db_mount.path,
                db_name=db_config.db_name,
                creation_statements=[
                    role_defs["create"].substitute(app_name=db_config.db_name)
                ],
                revocation_statements=[
                    role_defs["revoke"].substitute(app_name=db_config.db_name)
                ],
                max_ttl=db_config.max_ttl,
                default_ttl=db_config.default_ttl,
            )
        self.register_outputs({})

    def format_connection_string(
        self, db_config: OLVaultDatabaseConfig
    ) -> Union[Output[str], str]:
        if isinstance(db_config.db_host, Output):
            connection_url: Union[str, Output[str]] = db_config.db_host.apply(
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
    max_lease_ttl_seconds: int = ONE_MONTH_SECONDS * 6
    description: str
    aws_secret_key: str
    vault_backend_path: str
    policy_documents: dict[str, str]
    credential_type: str = "iam_user"

    @validator("vault_backend_path")
    def is_valid_path(cls, vault_backend_path: str) -> str:  # noqa: N805
        if vault_backend_path.startswith("/") or vault_backend_path.endswith("/"):
            raise ValueError(
                f"The specified path value {vault_backend_path} can not start or "
                "end with a slash"
            )
        return vault_backend_path


class OLVaultAWSSecretsEngine(ComponentResource):
    def __init__(
        self,
        engine_config: OLVaultAWSSecretsEngineConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:services:Vault:AWSSecretsEngine", engine_config.app_name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)  # type: ignore

        self.aws_secrets_engine = aws.SecretBackend(
            # TODO verify app_name exists based on Apps class in ol_types
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
                name=role_name,
                policy_document=json.dumps(policy),
                opts=resource_options,
            )

        self.register_outputs({})


class OLVaultPKIIntermediateCABackendConfig(BaseModel):
    max_ttl: int = ONE_MONTH_SECONDS * 12
    default_ttl: int = ONE_MONTH_SECONDS * 12
    # Stringified cert and private key generated by root CA to be used by intermediate
    # CA
    intermediate_ca_pem_bundle: str
    # Stringified public cert of intermediate CA
    intermediate_ca_cert: str


class OLVaultPKIIntermediateCABackend(ComponentResource):
    """
    PKI Intermediate CA Backend is set to pki-intermediate-ca.

    This should be used to create and configure the intermediate vault pki CA backend
    which is used to sign certificates used by the intermediate PKI's in the different
    environments.

    Offline CA --- pki-intermediate-ca --- sign -----> pki-intermediate-mitx-qa
                                                       pki-intermediate-mitxpro-qa
                                                       pki-intermeidate-...
    """

    def __init__(
        self,
        backend_config: OLVaultPKIIntermediateCABackendConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:services:Vault:PKI:IntermediateCABackend",
            "pki-intermediate-ca",
            None,
            opts,
        )

        resource_options = ResourceOptions(parent=self).merge(opts)  # type: ignore

        self.pki_intermediate_ca_backend = Mount(
            "pki-intermediate-ca",
            opts=resource_options,
            path="pki-intermediate-ca",
            type="pki",
            description="Backend to create certs for pki-intermediate-env backends",
            max_lease_ttl_seconds=backend_config.max_ttl,
            default_lease_ttl_seconds=backend_config.default_ttl,
        )

        self.pki_intermediate_ca_backend_config = pkisecret.SecretBackendConfigCa(
            "pki-intermediate-ca-backend-config",
            backend=self.pki_intermediate_ca_backend.id,
            pem_bundle=backend_config.intermediate_ca_pem_bundle,
        )

        self.pki_intermediate_ca_set_signed = (
            pkisecret.SecretBackendIntermediateSetSigned(
                "pki-intermediate-ca-set-signed",
                backend=self.pki_intermediate_ca_backend.id,
                certificate=backend_config.intermediate_ca_cert,
            )
        )

        self.pki_intermediate_ca_config_urls = pkisecret.SecretBackendConfigUrls(
            "pki-intermediate-ca-config-url",
            backend=self.pki_intermediate_ca_backend.id,
            crl_distribution_points=[
                f"{VAULT_API_URL}/backend_config.vault_intermediate_ca_backend_path/crl"
            ],
            issuing_certificates=[
                f"{VAULT_API_URL}/backend_config.vault_intermediate_ca_backend_path/ca"
            ],
        )

        self.register_outputs(
            {"pki_intermediate_ca": self.pki_intermediate_ca_backend.id}
        )


class OLVaultPKIIntermediateEnvBackendConfig(BaseModel):
    environment_name: str  # e.g. mitx-qa
    max_ttl: int = ONE_MONTH_SECONDS * 12
    default_ttl: int = ONE_MONTH_SECONDS * 12


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
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:services:Vault:PKI:IntermediateEnvBackendConfig",
            backend_config.environment_name,
            None,
            opts,
        )

        resource_options = ResourceOptions(parent=self).merge(opts)  # type: ignore

        self.pki_intermediate_environment_backend = Mount(
            f"pki-intermediate-{backend_config.environment_name}",
            opts=resource_options,
            path=f"pki-intermediate-{backend_config.environment_name}",
            type="pki",
            description="Backend to create certs for "
            f"pki-intermediate-{backend_config.environment_name} backends",
            max_lease_ttl_seconds=backend_config.max_ttl,
            default_lease_ttl_seconds=backend_config.default_ttl,
        )

        # Generate CSR for pki-intermediate-{env} backend
        self.pki_intermediate_envrionment_cert_request = (
            pkisecret.SecretBackendIntermediateCertRequest(
                f"pki-intermediate-{backend_config.environment_name}-csr",
                backend=self.pki_intermediate_environment_backend.id,
                common_name=f"pki-intermediate-{backend_config.environment_name} "
                "Intermediate Authority",
                type="internal",
                country=CERTIFICATE_CONFIG["country"],
                province=CERTIFICATE_CONFIG["state"],
                locality=CERTIFICATE_CONFIG["city"],
                organization=CERTIFICATE_CONFIG["organization"],
                ou=CERTIFICATE_CONFIG["organizational_unit"],
                postal_code=CERTIFICATE_CONFIG["zip_code"],
            )
        )

        # Sign genereated CSR for pki-intermediate-{env} backend by pki-intermediate-ca
        self.pki_intermediate_environment_signed_csr = (
            pkisecret.SecretBackendRootSignIntermediate(
                f"pki-intermediate-{backend_config.environment_name}-signed-csr",
                backend="pki-intermediate-ca",
                common_name=f"pki-intermediate-{backend_config.environment_name} "
                "Intermediate Authority",
                csr=self.pki_intermediate_envrionment_cert_request.csr,
            )
        )

        self.pki_intermediate_environment_set_signed = (
            pkisecret.SecretBackendIntermediateSetSigned(
                f"pki-intermediate-{backend_config.environment_name}-signed-cert",
                backend=self.pki_intermediate_environment_backend.id,
                certificate=self.pki_intermediate_environment_signed_csr.certificate,
            )
        )

        self.pki_intermediate_environment_config_urls = (
            pkisecret.SecretBackendConfigUrls(
                f"pki-intermediate-{backend_config.environment_name}-config-url",
                backend=self.pki_intermediate_environment_backend.id,
                crl_distribution_points=[
                    f"{VAULT_API_URL}/backend_confg.environment_name/crl"
                ],
                issuing_certificates=[
                    f"{VAULT_API_URL}/backend_confg.environment_name/ca"
                ],
            )
        )

        self.register_outputs({})


class OLVaultPKIIntermediateRoleConfig(BaseModel):
    pki_intermediate_backend: str
    role_name: str
    key_config: VaultPKIKeyTypeBits
    max_ttl: int = ONE_MONTH_SECONDS * 6
    default_ttl: int = ONE_MONTH_SECONDS * 6
    key_usages: list[str] = ["DigitalSignature", "KeyAgreement", "KeyEncipherment"]
    allowed_domains: list[str]
    cert_type: str  # Should be client or server

    @validator("cert_type")
    def is_valid_cert_type(cls, cert_type: str) -> str:  # noqa: N805
        if cert_type not in {"server", "client"}:
            raise ValueError(
                f"The specified certificate type {cert_type} has to be either client "
                "or server"
            )
        return cert_type


class OLVaultPKIIntermediateRole(ComponentResource):
    def __init__(
        self,
        role_config: OLVaultPKIIntermediateRoleConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:services:Vault:PKI:IntermediateRoleConfig",
            role_config.role_name,
            None,
            opts,
        )

        resource_options = ResourceOptions(parent=self).merge(opts)  # type: ignore

        # Default is True for both flags
        flag_type = {
            "client": {"client_flag": True, "server_flag": False},
            "server": {"server_flag": True, "client_flag": False},
        }

        self.pki_intermediate_env_client_role = pkisecret.SecretBackendRole(
            role_config.role_name,
            # forcing role name so that pulumi doesn't add suffix
            name=role_config.role_name,
            backend=role_config.pki_intermediate_backend,
            allowed_domains=role_config.allowed_domains,
            allow_glob_domains=True,
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
