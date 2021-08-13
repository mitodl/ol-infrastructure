import pulumi_vault as vault
from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

vault_config = Config("vault")
stack_info = parse_stack()

# TODO:
# - Mount AWS auth backend
# - Mount GitHub auth backend

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
