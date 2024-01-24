"""Creation and revocation statements used for Vault role definitions."""

# These strings are passed through the `.format` method so the variables that need to remain in the template  # noqa: E501
# to be passed to Vault are wrapped in 4 pairs of braces. TMM 2020-09-01

import json
from enum import Enum
from functools import lru_cache, partial
from pathlib import Path
from string import Template
from typing import Optional

import pulumi
import pulumi_vault
from bridge.secrets.sops import read_yaml_secrets

postgres_role_statements = {
    "approle": {
        "create": Template("CREATE ROLE ${app_name};"),
        "revoke": Template("DROP ROLE ${app_name};"),
    },
    "admin": {
        "create": Template(
            """CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
             VALID UNTIL '{{expiration}}' IN ROLE "rds_superuser"
             INHERIT CREATEROLE CREATEDB;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{{name}}"
             WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{{name}}"
             WITH GRANT OPTION;"""
        ),
        "revoke": Template(
            """REVOKE "${app_name}" FROM "{{name}}";
          GRANT CREATE ON SCHEMA public TO ${app_name} WITH GRANT OPTION;
          GRANT "{{name}}" TO ${app_name} WITH ADMIN OPTION;
          SET ROLE ${app_name};
          REASSIGN OWNED BY "{{name}}" TO "${app_name}";
          RESET ROLE;
          DROP OWNED BY "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          DROP USER "{{name}}";"""
        ),
    },
    "app": {
        "create": Template(
            """CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
            VALID UNTIL '{{expiration}}' IN ROLE "${app_name}" INHERIT;
          GRANT CREATE ON SCHEMA public TO ${app_name} WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "${app_name}"
             WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "${app_name}"
             WITH GRANT OPTION;
          SET ROLE "${app_name}";
          ALTER DEFAULT PRIVILEGES FOR ROLE "${app_name}" IN SCHEMA public
            GRANT ALL PRIVILEGES ON TABLES TO "${app_name}" WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR ROLE "${app_name}" IN SCHEMA public
            GRANT ALL PRIVILEGES ON SEQUENCES TO "${app_name}" WITH GRANT OPTION;
          RESET ROLE;
          ALTER ROLE "{{name}}" SET ROLE "${app_name}";"""
        ),
        "revoke": Template(
            """REVOKE "${app_name}" FROM "{{name}}";
          GRANT "{{name}}" TO ${app_name} WITH ADMIN OPTION;
          SET ROLE ${app_name};
          REASSIGN OWNED BY "{{name}}" TO "${app_name}";
          RESET ROLE;
          DROP OWNED BY "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          DROP USER "{{name}}";"""
        ),
    },
    "readonly": {
        "create": Template(
            """CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
             VALID UNTIL '{{expiration}}';
          GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{{name}}";
          GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{{name}}";
          SET ROLE "{{name}}";
          ALTER DEFAULT PRIVILEGES FOR USER "{{name}}" IN SCHEMA public GRANT SELECT
             ON TABLES TO "{{name}}";
          ALTER DEFAULT PRIVILEGES FOR USER "{{name}}" IN SCHEMA public GRANT SELECT
             ON SEQUENCES TO "{{name}}";
          RESET ROLE;"""
        ),
        "revoke": Template(
            """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          DROP USER "{{name}}";"""
        ),
    },
}

mysql_role_statements = {
    "admin": {
        "create": Template(
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX, "
            "ALTER, CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, CREATE VIEW, "
            "SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, EVENT, TRIGGER "
            "ON `%`.* TO '{{name}}' WITH GRANT OPTION; "
            "GRANT RELOAD, LOCK TABLES ON *.* to '{{name}}';"
        ),
        "revoke": Template("DROP USER '{{name}}';"),
    },
    "app": {
        "create": Template(
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, "
            "REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES "
            "ON ${app_name}.* TO '{{name}}'@'%';"
        ),
        "revoke": Template("DROP USER '{{name}}';"),
    },
    "readonly": {
        "create": Template(
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
            "GRANT SELECT, SHOW VIEW ON `%`.* TO '{{name}}'@'%';"
        ),
        "revoke": Template("DROP USER '{{name}}';"),
    },
}

mongodb_role_statements = {
    "admin": {
        "create": Template(
            json.dumps(
                {"roles": [{"role": "superuser"}, {"role": "root"}], "db": "admin"}
            )
        ),
        "revoke": Template(json.dumps({"db": "admin"})),
    },
    "app": {
        "create": Template(
            json.dumps({"roles": [{"role": "readWrite"}], "db": "${app_name}"})
        ),
        "revoke": Template(json.dumps({"db": "${app_name}"})),
    },
    "readonly": {
        "create": Template(json.dumps({"roles": [{"role": "read"}]})),
        "revoke": Template(""),
    },
}


class VaultPKIKeyTypeBits(int, Enum):
    rsa = 4096
    ec = 256


@lru_cache
def get_vault_provider(
    vault_address: str,
    vault_env_namespace: str,
    provider_name: Optional[str] = None,
    skip_child_token: Optional[bool] = None,
) -> pulumi.ResourceTransformationResult:
    pulumi_vault_creds = read_yaml_secrets(
        Path().joinpath(
            # We are forcing the assumption that the Vault cluster is in the operations
            # environment/namespace.(TMM 2021-10-19)
            "pulumi",
            f"vault.{vault_env_namespace}.yaml",
        )
    )
    return pulumi_vault.Provider(
        provider_name or "vault-provider",
        address=vault_address,
        add_address_to_env=True,
        skip_child_token=skip_child_token,
        token="",
        auth_login_userpass=pulumi_vault.ProviderAuthLoginUserpassArgs(
            mount="pulumi",
            username=pulumi_vault_creds["auth_username"],
            password=pulumi.Output.secret(pulumi_vault_creds["auth_password"]),
        ),
    )


def set_vault_provider(
    vault_address: str,
    vault_env_namespace: str,
    resource_args: pulumi.ResourceTransformationArgs,
    skip_child_token: Optional[bool] = None,
) -> pulumi.ResourceTransformationResult:
    if resource_args.type_.split(":")[0] == "vault":
        resource_args.opts.provider = get_vault_provider(
            vault_address,
            vault_env_namespace,
            skip_child_token=skip_child_token,
        )
    return pulumi.ResourceTransformationResult(
        props=resource_args.props,
        opts=resource_args.opts,
    )


def setup_vault_provider(skip_child_token: Optional[bool] = None):
    vault_address = pulumi.Config("vault").require("address")
    vault_env_namespace = pulumi.Config("vault_server").require("env_namespace")
    pulumi.runtime.register_stack_transformation(
        partial(
            set_vault_provider,
            vault_address,
            vault_env_namespace,
            skip_child_token=skip_child_token,
        )
    )
