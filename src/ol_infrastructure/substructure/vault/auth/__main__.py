import json
from pathlib import Path

from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import get_caller_identity, iam
from pulumi_vault import AuthBackend, AuthBackendTuneArgs, Policy, aws, github, jwt

from bridge.lib.magic_numbers import EIGHT_HOURS_SECONDS, ONE_MONTH_SECONDS
from ol_infrastructure.lib.ol_types import AWSBase, Environment
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

vault_config = Config("vault")
stack_info = parse_stack()
keycloak_config = Config("keycloak")

vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
aws_config = AWSBase(tags={"OU": "operations", "Environment": stack_info.name})
aws_account = get_caller_identity()

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

# Generic AWS backend
vault_aws_auth = AuthBackend(
    "vault-aws-auth-backend",
    type="aws",
)

vault_aws_auth_client = aws.AuthBackendClient(
    "vault-aws-auth-backend-client-configuration",
    backend=vault_aws_auth.path,
)

# Per environment backends
for env in Environment:
    vault_aws_auth = AuthBackend(
        f"vault-aws-auth-backend-{env.value}",
        type="aws",
        path=f"aws-{env.value}",
        description="AWS authentication via EC2 IAM",
        tune=AuthBackendTuneArgs(token_type="default-service"),  # noqa: S106
    )

    vault_aws_auth_client = aws.AuthBackendClient(
        f"vault-aws-auth-backend-client-configuration-{env.value}",
        backend=vault_aws_auth.path,
    )

vault_github_auth = github.AuthBackend(
    "vault-github-auth-backend",
    organization="mitodl",
    description="GitHub Auth mount in a Vault server",
    token_no_default_policy=True,
    token_ttl=ONE_MONTH_SECONDS,
    token_max_ttl=ONE_MONTH_SECONDS * 6,
)

# Enable OIDC auth method and configure it with Keycloak
vault_oidc_keycloak_auth = jwt.AuthBackend(
    "vault-oidc-keycloak-backend",
    path="oidc",
    type="oidc",
    description="OIDC auth Keycloak integration for vault client",
    oidc_discovery_url=f"{keycloak_config.get('url')}/realms/ol-platform-engineering",
    oidc_client_id=keycloak_config.get("client_id"),
    oidc_client_secret=keycloak_config.get("client_secret"),
    default_role="developer",
    opts=ResourceOptions(delete_before_replace=True),
)

# Developer policy definition
developer_policy = Policy(
    "developer-policy",
    name="developer",
    policy=Path(__file__)
    .parent.parent.joinpath("policies/developer/developer.hcl")
    .read_text(),
)

# Admin policy definition
admin_policy = Policy(
    "admin-policy",
    name="admin",
    policy=Path(__file__)
    .parent.parent.joinpath("policies/admin/admin.hcl")
    .read_text(),
)

# Configure OIDC developer role
developer_role = jwt.AuthBackendRole(
    "developer-role",
    backend=vault_oidc_keycloak_auth.path,
    role_name="developer",
    token_policies=[developer_policy.name],
    allowed_redirect_uris=[
        "http://localhost:8250/oidc/callback",
        f"{vault_config.get('address')}/ui/vault/auth/oidc/oidc/callback",
    ],
    bound_audiences=[keycloak_config.get("client_id")],
    user_claim="sub",
    role_type="oidc",
)

# Configure OIDC admin role
admin_role = jwt.AuthBackendRole(
    "admin-role",
    backend=vault_oidc_keycloak_auth.path,
    role_name="admin",
    token_policies=[admin_policy.name],
    allowed_redirect_uris=[
        "http://localhost:8250/oidc/callback",
        f"{vault_config.get('address')}/ui/vault/auth/oidc/oidc/callback",
    ],
    bound_audiences=[keycloak_config.get("client_id")],
    user_claim="sub",
    role_type="oidc",
    bound_claims={
        "realm_access.roles": "vault-admins"
    },  # Format as list with colon separator
)

# Raft Backup policy definition
raft_backup_policy = Policy(
    "raft-backup-policy",
    name="raft-backup",
    policy=Path(__file__).parent.joinpath("raft_backup_policy.hcl").read_text(),
)
# Register Vault Instance Profice + VPC for Vault AWS auth
aws.AuthBackendRole(
    "vault-raft-backup-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="raft-backup",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[
        vault_stack.require_output("vault_server")["instance_profile_arn"]
    ],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vault_stack.require_output("vault_server")["vpc_id"]],
    token_policies=[raft_backup_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)

# This is the shared developer role that will be assumed by developers using vault
if stack_info.name == "Production":
    eks_shared_developer_role = iam.Role(
        "eks-cluster-shared-developer-role",
        assume_role_policy=vault_stack.require_output("vault_server").apply(
            lambda vs: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": vs["instance_role_arn"]},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            )
        ),
        max_session_duration=EIGHT_HOURS_SECONDS,
    )

    eks_shared_developer_role_vault_backend_role = aws.SecretBackendRole(
        "eks-cluster-shared-developer-role-vault-backend-role",
        name="eks-cluster-shared-developer-role",
        backend="aws-mitx",
        credential_type="assumed_role",
        default_sts_ttl=EIGHT_HOURS_SECONDS,
        max_sts_ttl=EIGHT_HOURS_SECONDS,
        iam_tags={"OU": "operations", "environment": "production"},
        role_arns=[eks_shared_developer_role.arn],
        opts=ResourceOptions(delete_before_replace=True),
    )

    export("eks_shared_developer_role_arn", eks_shared_developer_role.arn)
