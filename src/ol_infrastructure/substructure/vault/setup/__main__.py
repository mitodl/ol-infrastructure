import time
from pathlib import Path
from typing import List

import hvac
import pulumi
import pulumi_vault as vault

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import get_vault_provider

vault_config = pulumi.Config("vault_setup")
vault_server_config = pulumi.Config("vault_server")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
env_namespace = f"{stack_info.env_prefix}.{stack_info.env_suffix}"
vault_cluster = pulumi.StackReference(
    f"infrastructure.vault.{stack_info.env_prefix}.{stack_info.name}"
)
vault_dns = vault_cluster.outputs["vault_server"]["public_dns"]
vault_address = vault_cluster.outputs["vault_server"]["cluster_address"]
key_shares = vault_config.get_int("key_shares") or 3
recovery_threshold = vault_config.get_int("recovery_threshold") or 2
pgp_public_keys: List[str] = vault_config.get_object("pgp_keys")

if pgp_public_keys and len(pgp_public_keys) != key_shares:
    raise ValueError("The number of PGP keys needs to match the number of key shares.")

PULUMI = "pulumi"
pulumi_vault_creds = read_yaml_secrets(
    Path().joinpath(
        PULUMI, f"vault.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml"
    )
)


def init_vault_cluster(vault_addr):  # noqa: WPS231
    vault_client = hvac.Client(url=f"https://{vault_addr}")
    recovery_keys = []
    if not vault_client.sys.is_initialized():
        init_response = vault_client.sys.initialize(
            secret_shares=key_shares,
            stored_shares=key_shares,
            recovery_shares=key_shares,
            recovery_threshold=recovery_threshold,
            recovery_pgp_keys=pgp_public_keys,
        )

        vault_root_token = init_response["root_token"]
        recovery_keys = init_response["recovery_keys"]

        vault_client.token = vault_root_token  # noqa: WPS428
        pulumi.log.info(
            "IMPORTANT!: Retain the keys in vault_recovery_keys.txt for "
            "recovering the cluster."
        )

        with open("vault_recovery_keys.txt", "w") as vault_recovery_keys_file:
            vault_recovery_keys_file.write("\n".join(recovery_keys))

        vault_ready = False
        while not vault_ready:
            try:  # noqa: WPS229
                pulumi.log.info(
                    "Trying to enable the userpass backend",
                    vault_client.sys.is_sealed(),
                )
                enabled_auths = vault_client.sys.list_auth_methods()
                if f"{PULUMI}/" not in enabled_auths.keys():
                    vault_client.sys.enable_auth_method(
                        method_type="userpass",
                        description="Allow authentication for Pulumi using the "
                        "user/pass method",
                        path=PULUMI,
                    )

                vault_client.sys.create_or_update_policy(
                    name="cluster-admin",
                    policy=Path(__file__)
                    .resolve()
                    .parent.joinpath("pulumi_policy.hcl")
                    .read_text(),
                )

                user_pass = hvac.api.auth_methods.userpass.Userpass(
                    vault_client.adapter
                )
                user_pass.create_or_update_user(
                    username=pulumi_vault_creds["auth_username"],
                    password=pulumi_vault_creds["auth_password"],
                    mount_point=PULUMI,
                    policies=["cluster-admin", PULUMI],
                )

                vault_client.revoke_self_token()
                vault_ready = True
            except (hvac.exceptions.VaultDown, hvac.exceptions.InternalServerError):
                pulumi.log.info("Vault isn't ready yet. Waiting and trying again.")
                time.sleep(1)
    return recovery_keys


vault_recovery_keys = vault_dns.apply(init_vault_cluster)

vault_provider = pulumi.ResourceOptions(
    provider=get_vault_provider(
        vault_address=vault_address,
        vault_env_namespace=vault_server_config.get("env_namespace")
        or f"operations.{stack_info.env_suffix}",
    )
)

vault_syslog_audit = vault.Audit(
    "vault-server-syslog-audit-device",
    type="syslog",
    description="Vault syslog audit record",
    options={"format": "json"},
    opts=vault_provider,
)

vault_file_audit = vault.Audit(
    "vault-server-file-audit-device",
    type="file",
    description="Vault file based audit record to stdout for JournalD",
    options={"file_path": "stdout", "format": "json"},
    opts=vault_provider,
)

vault_pulumi_policy = vault.Policy(
    "vault-policy-for-pulumi",
    name=PULUMI,
    policy=Path(__file__).parent.joinpath("pulumi_policy.hcl").read_text(),
    opts=vault_provider,
)

vault_user_pass_auth = vault.AuthBackend(
    "vault-user-auth-backend",
    type="userpass",
    description="Username and password based authentication for Vault",
    tune=vault.AuthBackendTuneArgs(token_type="default-service"),  # noqa: S106
    opts=vault_provider,
)
