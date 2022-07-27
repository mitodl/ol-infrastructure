from pathlib import Path

from pulumi import Config
from pulumi_vault import AuthBackend, AuthBackendTuneArgs, Policy, aws, github

from bridge.lib.magic_numbers import ONE_MONTH_SECONDS, SIX_MONTHS
from ol_infrastructure.lib.ol_types import Environment
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

vault_config = Config("vault")
stack_info = parse_stack()

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
        tune=AuthBackendTuneArgs(token_type="default-service"),
    )

    vault_aws_auth_client = aws.AuthBackendClient(
        f"vault-aws-auth-backend-client-configuration-{env.value}",
        backend=vault_aws_auth.path,
    )

# GitHub auth backend
vault_github_auth = github.AuthBackend(
    "vault-github-auth-backend",
    organization="mitodl",
    description="GitHub Auth mount in a Vault server",
    token_no_default_policy=True,
    token_ttl=ONE_MONTH_SECONDS,
    token_max_ttl=SIX_MONTHS,
)

if stack_info.env_suffix != "production":
    policy_folder = (Path(__file__).resolve()).parent.parent.joinpath(
        "policies/github/"
    )
    for hcl_file in policy_folder.iterdir():
        if "software_engineer.hcl" in hcl_file.name:
            software_engineer_policy_file = open(hcl_file).read()
            software_engineer_policy = Policy(
                "github-auth-software-engineer", policy=software_engineer_policy_file
            )
            for team in ["odl-engineering", "arbisoft-contractors"]:
                vault_github_auth_team = github.Team(
                    f"vault-github-auth-{team}",
                    team=team,
                    policies=["software-engineer"],
                )
        if "admin.hcl" in hcl_file.name:
            devops_policy_file = open(hcl_file).read()
            devops_policy = Policy("github-auth-devops", policy=devops_policy_file)
            for team in ["devops"]:
                vault_github_auth_team = github.Team(
                    f"vault-github-auth-{team}", team=team, policies=["admin"]
                )
