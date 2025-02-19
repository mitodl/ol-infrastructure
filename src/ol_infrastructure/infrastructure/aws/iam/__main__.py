import json

from pulumi import ResourceOptions, StackReference, export
from pulumi_aws import get_caller_identity, iam

from ol_infrastructure.lib.aws.iam_helper import (
    ADMIN_USERNAMES,
    EKS_ADMIN_USERNAMES,
    EKS_DEVELOPER_USERNAMES,
    IAM_POLICY_VERSION,
)

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
concourse_production_stack = StackReference("applications.concourse.Production")

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
