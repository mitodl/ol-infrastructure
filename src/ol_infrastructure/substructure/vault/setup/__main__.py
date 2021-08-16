from pathlib import Path

import pulumi_vault as vault
from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

vault_config = Config("vault")
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# TODO:
# - Create audit device
# - Create orphan token to be used by Pulumi
# - Create Pulumi policy for Vault
# - Revoke initial root token

vault_syslog_audit = vault.Audit(
    "vault-server-syslog-audit-device",
    type="syslog",
    description="Vault syslog audit record",
    options={"format": "json"},
)

vault_file_audit = vault.Audit(
    "vault-server-file-audit-device",
    type="file",
    description="Vault file based audit record to stdout for JournalD",
    options={"file_path": "stdout", "format": "json"},
)

vault_pulumi_policy = vault.Policy(
    "vault-policy-for-pulumi",
    name="pulumi",
    policy=Path(__file__).parent.joinpath("pulumi_policy.hcl").read_text(),
)

vault_user_pass_auth = vault.AuthBackend(
    "vault-user-auth-backend",
    type="userpass",
    description="Username and password based authentication for Vault",
    tune=vault.AuthBackendTuneArgs(token_type="default-service"),
)
