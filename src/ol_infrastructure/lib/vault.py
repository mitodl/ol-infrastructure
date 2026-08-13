# ruff: noqa: E501
"""Creation and revocation statements used for Vault role definitions."""

# These strings are passed through the `.format` method so the variables that need to remain in the template
# to be passed to Vault are wrapped in 4 pairs of braces. TMM 2020-09-01

import json
from enum import Enum
from functools import lru_cache, partial
from pathlib import Path
from string import Template

import pulumi
import pulumi_vault

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import StackInfo

postgres_role_statements = {
    "admin": {
        "create": [  # We use the prexisting rds_superuser role for admin users
            Template(
                """
                CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
                VALID UNTIL '{{expiration}}' IN ROLE "rds_superuser"
                INHERIT CREATEROLE CREATEDB;
                """
            ),
            Template(
                """
                GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{{name}}"
                WITH GRANT OPTION;
                """
            ),
            Template(
                """
                GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{{name}}"
                WITH GRANT OPTION;
                """
            ),
        ],
        "revoke": [
            # Remove the user from the pre-existing rds_superuser role
            Template("""REVOKE "rds_superuser" FROM "{{name}}";"""),
            # Change ownership to the app role for anything that might belong to this user
            Template("""SET ROLE ${app_name};"""),
            Template("""REASSIGN OWNED BY "{{name}}" TO "${app_name}";"""),
            Template("""RESET ROLE;"""),
            # Take any permissions assigned directly to this user away
            Template(
                """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";"""
            ),
            Template(
                """REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";"""
            ),
            Template("""REVOKE USAGE ON SCHEMA public FROM "{{name}}";"""),
            # Finally, drop this user from the database
            Template("""DROP USER "{{name}}";"""),
        ],
        "renew": [],
        "rollback": [],
    },
    "app": {
        "create": [
            # Check if the role exists and create it if not
            Template(
                """
                DO
                $$do$$
                BEGIN
                   IF EXISTS (
                      SELECT FROM pg_catalog.pg_roles
                      WHERE  rolname = '${app_name}') THEN
                          RAISE NOTICE 'Role "${app_name}" already exists. Skipping.';
                   ELSE
                      BEGIN   -- nested block
                         CREATE ROLE ${app_name};
                      EXCEPTION
                         WHEN duplicate_object THEN
                            RAISE NOTICE 'Role "${app_name}" was just created by a concurrent transaction. Skipping.';
                      END;
                   END IF;
                END
                $$do$$;
                """
            ),
            # Set/refresh the default privileges for the new role
            Template(
                """GRANT CREATE ON SCHEMA public TO ${app_name} WITH GRANT OPTION;"""
            ),
            Template("""
                GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "${app_name}"
                WITH GRANT OPTION;
                """),
            Template(
                """
                GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "${app_name}"
                WITH GRANT OPTION;
                """
            ),
            Template("""SET ROLE "${app_name}";"""),
            Template(
                """
                ALTER DEFAULT PRIVILEGES FOR ROLE "${app_name}" IN SCHEMA public
                GRANT ALL PRIVILEGES ON TABLES TO "${app_name}" WITH GRANT OPTION;
                """
            ),
            Template(
                """
                ALTER DEFAULT PRIVILEGES FOR ROLE "${app_name}" IN SCHEMA public
                GRANT ALL PRIVILEGES ON SEQUENCES TO "${app_name}" WITH GRANT OPTION;
                """
            ),
            Template("""RESET ROLE;"""),
            # Create the user in ${app_name}
            Template(
                """
                CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
                VALID UNTIL '{{expiration}}' IN ROLE "${app_name}" INHERIT;
                """
            ),
            # Make sure things done by the new user belong to role and not the user
            Template("""ALTER ROLE "{{name}}" SET ROLE "${app_name}";"""),
        ],
        "revoke": [
            # Remove the user from the app role
            Template("""REVOKE "${app_name}" FROM "{{name}}";"""),
            # Put the user back into the app role but as an administrator
            Template("""GRANT "{{name}}" TO ${app_name} WITH ADMIN OPTION;"""),
            # Change ownership to the app role for anything that might belong to this user
            Template("""SET ROLE ${app_name};"""),
            Template("""REASSIGN OWNED BY "{{name}}" TO "${app_name}";"""),
            Template("""RESET ROLE;"""),
            # Take any permissions assigned directly to this user away
            Template(
                """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";"""
            ),
            Template(
                """REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";"""
            ),
            Template("""REVOKE USAGE ON SCHEMA public FROM "{{name}}";"""),
            # Finally, drop this user from the database
            Template("""DROP USER "{{name}}";"""),
        ],
        "renew": [],
        "rollback": [],
    },
    "readonly": {
        "create": [
            # Check if the role exists and create it if not
            Template(
                """
                DO
                $$do$$
                BEGIN
                   IF EXISTS (
                      SELECT FROM pg_catalog.pg_roles
                      WHERE  rolname = 'read_only_role') THEN
                          RAISE NOTICE 'Role "read_only_role" already exists. Skipping.';
                   ELSE
                      BEGIN   -- nested block
                         CREATE ROLE read_only_role;
                      EXCEPTION
                         WHEN duplicate_object THEN
                            RAISE NOTICE 'Role "read_only_role" was just created by a concurrent transaction. Skipping.';
                      END;
                   END IF;
                END
                $$do$$;
                """
            ),
            # Set/refresh the default privileges for the new role
            Template(
                """
                GRANT SELECT ON ALL TABLES IN SCHEMA public TO "read_only_role";
                """
            ),
            Template(
                """
                GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "read_only_role";
                """
            ),
            Template("""SET ROLE "read_only_role";"""),
            Template(
                """
                ALTER DEFAULT PRIVILEGES FOR USER "read_only_role" IN SCHEMA public GRANT SELECT
                ON TABLES TO "read_only_role";
                """
            ),
            Template(
                """
                ALTER DEFAULT PRIVILEGES FOR USER "read_only_role" IN SCHEMA public GRANT SELECT
                ON SEQUENCES TO "read_only_role";
                """
            ),
            Template("""RESET ROLE;"""),
            # Self-healing cleanup: earlier versions of this role unconditionally
            # granted rds_iam here, and that membership is persisted on
            # "read_only_role" in the database itself, not in this statement list -
            # removing the old GRANT from creation_statements does not retroactively
            # revoke it. Since these statements run on every readonly credential
            # issuance, this guarantees any stale grant is cleared out the next time
            # anyone requests a readonly credential. REVOKE-ing a membership that
            # doesn't exist is a no-op (NOTICE, not an error), so this is safe to run
            # unconditionally. Opt-in consumers append
            # RDS_IAM_READONLY_GRANT_STATEMENT after this in their own copy, so their
            # GRANT still wins.
            Template(
                """
                DO
                $$do$$
                BEGIN
                    IF EXISTS (
                        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rds_iam'
                    ) THEN
                        REVOKE rds_iam FROM "read_only_role";
                    END IF;
                END
                $$do$$;
                """
            ),
            # Create the read-only user and put it into the read-only-role
            Template(
                """
                CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
                VALID UNTIL '{{expiration}}' IN ROLE "read_only_role" INHERIT;
                """
            ),
            # Make sure things done by the new user belong to role and not the user
            Template("""ALTER ROLE "{{name}}" SET ROLE "read_only_role";"""),
        ],
        "revoke": [
            # Remove the user from the app role
            Template("""REVOKE "read_only_role" FROM "{{name}}";"""),
            # Put the user back into the app role but as an administrator
            Template("""GRANT "{{name}}" TO read_only_role WITH ADMIN OPTION;"""),
            # Change ownership to the app role for anything that might belong to this user
            Template("""SET ROLE read_only_role;"""),
            Template("""REASSIGN OWNED BY "{{name}}" TO "read_only_role";"""),
            Template("""RESET ROLE;"""),
            # Take any permissions assigned directly to this user away
            Template(
                """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";"""
            ),
            Template(
                """REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";"""
            ),
            Template("""REVOKE USAGE ON SCHEMA public FROM "{{name}}";"""),
            # Finally, drop this user from the database
            Template("""DROP USER "{{name}}";"""),
        ],
        "renew": [],
        "rollback": [],
    },
}

# Opt-in addition for postgres_role_statements["readonly"]["create"] on
# instances where a consumer specifically authenticates via AWS IAM tokens
# (e.g. rds-db:connect) rather than a Vault-issued password. On RDS Postgres,
# once a role is a member of rds_iam, RDS enforces IAM-token auth for that
# role and rejects plain password auth with "FATAL: PAM authentication
# failed" - so this must NOT be added to postgres_role_statements directly.
# Use with_rds_iam_readonly_grant() below rather than appending it by hand:
# postgres_role_statements.copy() is a shallow copy, so mutating
# ["readonly"]["create"] in place (e.g. via .append()) would mutate the
# shared defaults too.
RDS_IAM_READONLY_GRANT_STATEMENT = Template(
    """
    DO
    $$do$$
    BEGIN
        IF EXISTS (
            SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rds_iam'
        ) THEN
            GRANT rds_iam TO "read_only_role";
        END IF;
    END
    $$do$$;
    """
)


def with_rds_iam_readonly_grant(
    role_statements: dict[str, dict[str, list[Template]]],
) -> dict[str, dict[str, list[Template]]]:
    """Return a copy of role_statements with readonly granted rds_iam.

    Does not mutate the input. Only use this for a database whose readonly
    consumers authenticate via AWS IAM tokens - see
    RDS_IAM_READONLY_GRANT_STATEMENT above for why.
    """
    return {
        **role_statements,
        "readonly": {
            **role_statements["readonly"],
            "create": [
                *role_statements["readonly"]["create"],
                RDS_IAM_READONLY_GRANT_STATEMENT,
            ],
        },
    }


mysql_role_statements = {
    "admin": {
        "create": [
            Template("""CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"""),
            Template(
                """GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX,
                ALTER, CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, CREATE VIEW,
                SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, EVENT, TRIGGER
                ON `%`.* TO '{{name}}' WITH GRANT OPTION;"""
            ),
            Template("""GRANT RELOAD, LOCK TABLES ON *.* to '{{name}}';"""),
        ],
        "revoke": [Template("""DROP USER '{{name}}';""")],
        "renew": [],
        "rollback": [],
    },
    "app": {
        "create": [
            Template("""CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"""),
            Template(
                """GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER,
                REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES
                ON ${app_name}.* TO '{{name}}'@'%';"""
            ),
        ],
        "revoke": [Template("DROP USER '{{name}}';")],
        "renew": [],
        "rollback": [],
    },
    "readonly": {
        "create": [
            Template(
                """CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';
                GRANT SELECT, SHOW VIEW ON `%`.* TO '{{name}}'@'%';"""
            ),
        ],
        "revoke": [Template("DROP USER '{{name}}';")],
        "renew": [],
        "rollback": [],
    },
}

mongodb_role_statements = {
    "admin": {
        "create": [
            Template(
                json.dumps(
                    {"roles": [{"role": "superuser"}, {"role": "root"}], "db": "admin"}
                )
            )
        ],
        "revoke": [Template(json.dumps({"db": "admin"}))],
        "renew": [],
        "rollback": [],
    },
    "app": {
        "create": [
            Template(
                json.dumps({"roles": [{"role": "readWrite"}], "db": "${app_name}"})
            )
        ],
        "revoke": [Template(json.dumps({"db": "${app_name}"}))],
        "renew": [],
        "rollback": [],
    },
    "readonly": {
        "create": [Template(json.dumps({"roles": [{"role": "read"}]}))],
        "revoke": [Template("")],
        "renew": [],
        "rollback": [],
    },
}


class VaultPKIKeyTypeBits(int, Enum):
    rsa = 4096
    ec = 256


@lru_cache
def get_vault_provider(
    vault_address: str,
    vault_env_namespace: str,
    provider_name: str | None = None,
    skip_child_token: bool | None = None,  # noqa: FBT001
) -> pulumi_vault.Provider:
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
    skip_child_token: bool | None = None,  # noqa: FBT001
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


def setup_vault_provider(
    stack_info: StackInfo | None = None,
    *,
    skip_child_token: bool | None = None,
) -> pulumi_vault.Provider:
    if stack_info:
        vault_address = f"https://vault-{stack_info.env_suffix}.odl.mit.edu"
        vault_env_namespace = f"operations.{stack_info.env_suffix}"
    else:
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
    return get_vault_provider(
        vault_address, vault_env_namespace, skip_child_token=skip_child_token
    )
