from pulumi import export
from pulumi_aws import iam

administrator_iam_group = iam.Group(
    "administrators-iam-group",
    name="Admins",
)

administrator_iam_group_membership = iam.GroupMembership(
    "administrators-iam-group-membership",
    group=administrator_iam_group.name,
    users=[
        "cpatti",
        "ferdial",
        "ichuang",
        "mas48",
        "pdpinch",
        "qhoque",
        "shaidar",
        "tmacey",
    ],
)
administrator_export_dict = {
    "arn": administrator_iam_group.arn,
    "name": administrator_iam_group.name,
}

export("administrators", administrator_export_dict)
