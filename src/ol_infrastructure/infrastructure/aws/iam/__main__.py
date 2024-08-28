from pulumi import export
from pulumi_aws import iam

from ol_infrastructure.lib.aws.iam_helper import ADMIN_USERNAMES, EKS_ADMIN_USERNAMES

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
