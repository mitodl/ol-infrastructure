import json

from pulumi import StackReference, export
from pulumi_aws import get_caller_identity, iam

from ol_infrastructure.lib.aws.iam_helper import (
    ADMIN_USERNAMES,
    EKS_ADMIN_USERNAMES,
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
    "eks-amdministrators-iam-group-membership",
    group=eks_administrator_iam_group.name,
    users=EKS_ADMIN_USERNAMES,
)

eks_administrator_export_dict = {
    "arn": eks_administrator_iam_group.arn,
    "name": eks_administrator_iam_group.name,
}

export("eks_administrators", eks_administrator_export_dict)

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
