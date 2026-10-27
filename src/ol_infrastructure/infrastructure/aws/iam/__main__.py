import json

from pulumi import ResourceOptions, export
from pulumi_aws import get_caller_identity, iam

from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.iam_helper import (
    ADMIN_USERNAMES,
    BEDROCK_PARLIAMENT_CONFIG,
    EKS_ADMIN_USERNAMES,
    EKS_DEVELOPER_USERNAMES,
    IAM_POLICY_VERSION,
    bedrock_invoke_statements,
    lint_iam_policy,
)
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit, Environment
from ol_infrastructure.lib.pulumi_helper import make_stack_reference

administrator_iam_group = iam.Group(
    "administrators-iam-group",
    name="Admins",
)
administrator_iam_group_membership = iam.GroupMembership(
    "administrators-iam-group-membership",
    group=administrator_iam_group.name,
    users=ADMIN_USERNAMES,
)
administrator_export_dict = {
    "arn": administrator_iam_group.arn,
    "name": administrator_iam_group.name,
}
export("administrators", administrator_export_dict)


eks_administrator_iam_group = iam.Group(
    "eks-administrators-iam-group",
    name="EKSAdmins",
)
eks_administrator_iam_group_membership = iam.GroupMembership(
    "eks-administrators-iam-group-membership",
    group=eks_administrator_iam_group.name,
    users=EKS_ADMIN_USERNAMES,
)
eks_administrator_export_dict = {
    "arn": eks_administrator_iam_group.arn,
    "name": eks_administrator_iam_group.name,
}
export("eks_administrators", eks_administrator_export_dict)

# This developer group and these accounts don't actually serve a purpose
# at this time but they might be useful some day
eks_developers_iam_group = iam.Group(
    "eks-developers-iam-group",
    name="EKSDevelopers",
)
eks_developers_iam_group_membership = iam.GroupMembership(
    "eks-developers-iam-group-membership",
    group=eks_developers_iam_group.name,
    users=EKS_DEVELOPER_USERNAMES,
)
eks_developers_export_dict = {
    "arn": eks_developers_iam_group.arn,
    "name": eks_developers_iam_group.name,
}
export("eks_developers", eks_developers_export_dict)

# In the case of developers, we're actually going to create and manage them with pulumi
# so we can ensure they have the tags they need for EKS access
for developer in EKS_DEVELOPER_USERNAMES:
    iam.User(
        f"eks-developers-{developer}-iam-user",
        name=developer,
        opts=ResourceOptions(
            import_=developer,  # Pull in any manually created users (Provided they have no tags yet)  # noqa: E501
        ),
        tags={"team": "eks-developers"},
    )


account_id = get_caller_identity().account_id
concourse_production_stack = make_stack_reference(projects.CONCOURSE, "Production")

# The people or nodes assuming this role already have admin powers
eks_cluster_creator_role = iam.Role(
    "eks-cluster-creator-role",
    assume_role_policy=concourse_production_stack.require_output(
        "infra-instance-role-arn"
    ).apply(
        lambda concourse_arn: json.dumps(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {
                            "Service": "ec2.amazonaws.com",
                            "AWS": [
                                f"arn:aws:iam::{account_id}:user/{username}"
                                for username in EKS_ADMIN_USERNAMES
                            ]
                            + [concourse_arn],
                        },
                    }
                ],
            }
        )
    ),
    path="/ol-infrastructure/eks/shared/",
    managed_policy_arns=["arn:aws:iam::aws:policy/AdministratorAccess"],
)

export("eks_cluster_creator_role_arn", eks_cluster_creator_role.arn)

# GitHub Actions jobs exchange their OIDC token for credentials of a role that
# trusts this provider. AWS validates token.actions.githubusercontent.com
# against its own trusted CAs, so no thumbprint is configured.
GITHUB_ACTIONS_OIDC_ISSUER = "token.actions.githubusercontent.com"
GITHUB_ACTIONS_OIDC_AUDIENCE = "sts.amazonaws.com"
github_actions_oidc_provider = iam.OpenIdConnectProvider(
    "github-actions-oidc-provider",
    url=f"https://{GITHUB_ACTIONS_OIDC_ISSUER}",
    client_id_lists=[GITHUB_ACTIONS_OIDC_AUDIENCE],
)
export("github_actions_oidc_provider_arn", github_actions_oidc_provider.arn)

# The evaluation gate in mitodl/ol-data-platform calls Bedrock for the model
# under test and the judge.
# https://github.com/mitodl/ol-llm/blob/main/docs/evaluation-gate.md
# It runs ol-llm's reusable workflow, whose token carries the caller's sub:
# pull requests, and pushes and manual runs on main.
evaluation_gate_token_conditions = {
    f"{GITHUB_ACTIONS_OIDC_ISSUER}:aud": GITHUB_ACTIONS_OIDC_AUDIENCE,
    f"{GITHUB_ACTIONS_OIDC_ISSUER}:sub": [
        "repo:mitodl/ol-data-platform:pull_request",
        "repo:mitodl/ol-data-platform:ref:refs/heads/main",
    ],
}
ol_data_platform_evaluation_gate_role = iam.Role(
    "ol-data-platform-evaluation-gate-role",
    name="ol-data-platform-evaluation-gate",
    path="/ol-infrastructure/github-actions/",
    description="Bedrock access for the ol-data-platform evaluation gate workflow.",
    assume_role_policy=github_actions_oidc_provider.arn.apply(
        lambda provider_arn: json.dumps(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Principal": {"Federated": provider_arn},
                        "Condition": {"StringEquals": evaluation_gate_token_conditions},
                    }
                ],
            }
        )
    ),
    tags=AWSBase(tags={"OU": BusinessUnit.data, "Environment": Environment.data}).tags,
)
evaluation_gate_bedrock_policy = {
    "Version": IAM_POLICY_VERSION,
    "Statement": bedrock_invoke_statements(
        account_id,
        inference_profiles=[
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-sonnet-5",
        ],
    ),
}
lint_iam_policy(
    evaluation_gate_bedrock_policy, parliament_config=BEDROCK_PARLIAMENT_CONFIG
)
# A managed policy rather than an inline one: the Concourse worker that deploys
# this stack may create policies and attach them under /ol-infrastructure/, but
# has no iam:PutRolePolicy.
evaluation_gate_bedrock_iam_policy = iam.Policy(
    "ol-data-platform-evaluation-gate-bedrock-policy",
    name="ol-data-platform-evaluation-gate-bedrock",
    path="/ol-infrastructure/github-actions/",
    description="Bedrock invoke for the ol-data-platform evaluation gate workflow.",
    policy=json.dumps(evaluation_gate_bedrock_policy),
    tags=AWSBase(tags={"OU": BusinessUnit.data, "Environment": Environment.data}).tags,
)
iam.RolePolicyAttachment(
    "ol-data-platform-evaluation-gate-bedrock-policy-attachment",
    role=ol_data_platform_evaluation_gate_role.name,
    policy_arn=evaluation_gate_bedrock_iam_policy.arn,
)
export(
    "ol_data_platform_evaluation_gate_role_arn",
    ol_data_platform_evaluation_gate_role.arn,
)
