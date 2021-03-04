from pulumi import Config, StackReference, export
from pulumi_aws import iam
from pulumi_vault import aws

from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.pulumi_helper import parse_stack

mit_open_config = Config("mit_open")
stack_info = parse_stack()
data_warehouse_stack = StackReference(
    f"infrastructure.aws.data_warehouse.{stack_info.name}"
)
athena_warehouse_outputs = data_warehouse_stack.require_output("athena_data_warehouse")
athena_warehouse_workgroup = athena_warehouse_outputs["workgroup"]

open_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "athena:ListDataCatalogs",
                "athena:ListWorkGroups",
            ],
            "Resource": ["*"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "athena:BatchGetNamedQuery",
                "athena:BatchGetQueryExecution",
                "athena:GetNamedQuery",
                "athena:GetQueryExecution",
                "athena:GetQueryResults",
                "athena:GetQueryResultsStream",
                "athena:GetWorkGroup",
                "athena:ListNamedQueries",
                "athena:ListQueryExecutions",
                "athena:StartQueryExecution",
                "athena:StopQueryExecution",
            ],
            "Resource": [f"arn:*:athena:*:*:workgroup/{athena_warehouse_workgroup}"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "athena:GetDataCatalog",
                "athena:GetDatabase",
                "athena:GetTableMetadata",
                "athena:ListDatabases",
                "athena:ListTableMetadata",
            ],
            "Resource": ["arn:*:athena:*:*:datacatalog/*"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "glue:BatchGetPartition",
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetPartition",
                "glue:GetPartitions",
                "glue:GetTable",
                "glue:GetTables",
            ],
            "Resource": [
                "arn:aws:glue:*:*:catalog",
                "arn:aws:glue:*:*:database/*{stack_info.env_suffix}",
                "arn:aws:glue:*:*:table/*{stack_info.env_suffix}/*",
            ],
        },
    ],
}

mit_open_iam_policy = iam.Policy(
    f"mit_open_iam_permissions_{stack_info.env_suffix}",
    name=f"mit-open-application-permissions-{stack_info.env_suffix}",
    path="/ol-applications/mit-open/{stack_info.env_suffix}/",
    policy=lint_iam_policy(open_policy_document, stringify=True),
)

mit_open_vault_iam_role = aws.SecretBackendRole(
    f"mit-open-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name="mit-open-application-{stack_info.env_suffix}",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[mit_open_iam_policy.arn],
)

export(
    "mit_open",
    {
        "iam_policy": mit_open_iam_policy.arn,
        "vault_iam_role": f"{mit_open_vault_iam_role.backend}/roles/{mit_open_vault_iam_role.name}",
    },
)
