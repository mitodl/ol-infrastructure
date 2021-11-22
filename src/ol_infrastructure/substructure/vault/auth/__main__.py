import pulumi_vault as vault
from pulumi import Config

from ol_infrastructure.lib.ol_types import Environment
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

vault_config = Config("vault")
stack_info = parse_stack()

setup_vault_provider()

# TODO:
# - Mount AWS auth backend
# - Mount GitHub auth backend

# Generic AWS backend
vault_aws_auth = vault.AuthBackend(
    "vault-aws-auth-backend",
    type="aws",
    description="AWS authentication via EC2 IAM",
    tune=vault.AuthBackendTuneArgs(token_type="default-service"),
)

vault_aws_auth_client = vault.aws.AuthBackendClient(
    "vault-aws-auth-backend-client-configuration",
    backend=vault_aws_auth.path,
)

# Per environment backends
for env in Environment:
    vault_aws_auth = vault.AuthBackend(
        f"vault-aws-auth-backend-{env.value}",
        type="aws",
        path=f"aws-{env.value}",
        description="AWS authentication via EC2 IAM",
        tune=vault.AuthBackendTuneArgs(token_type="default-service"),
    )

    vault_aws_auth_client = vault.aws.AuthBackendClient(
        f"vault-aws-auth-backend-client-configuration-{env.value}",
        backend=vault_aws_auth.path,
    )
