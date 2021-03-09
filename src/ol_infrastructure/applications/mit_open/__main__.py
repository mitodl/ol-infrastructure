from pulumi import Config, export
from pulumi.output import Output
from pulumi_aws import iam, s3
from pulumi_vault import aws

from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

mit_open_config = Config("mit_open")
stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "mit-open",
        "Environment": stack_info.env_suffix,
        "Application": "mit-open",
    }
)
app_env_suffix = {"ci": "ci", "qa": "rc", "production": "production"}[
    stack_info.env_suffix
]

app_storage_bucket_name = f"mit-open-app-storage-{app_env_suffix}"
application_storage_bucket = s3.Bucket(
    f"mit_open_learning_application_storage_bucket_{stack_info.env_suffix}",
    bucket=app_storage_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

course_data_bucket = s3.Bucket(
    f"mit-open-learning-course-data-{stack_info.env_suffix}",
    bucket=f"open-learning-course-data-{app_env_suffix}",
    versioning=s3.BucketVersioningArgs(enabled=True),
    cors_rules=[
        s3.BucketCorsRuleArgs(
            allowed_methods=["GET"],
            allowed_headers=["*"],
            allowed_origins=["*"],
            max_age_seconds=300,
        )
    ],
    tags=aws_config.tags,
)

s3_bucket_permissions = [
    {
        "Action": [
            "s3:GetObject*",
            "s3:ListBucket*",
            "s3:PutObject",
            "S3:DeleteObject",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}",
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}/*",
            f"arn:aws:s3:::{app_storage_bucket_name}",
            f"arn:aws:s3:::{app_storage_bucket_name}/*",
            f"arn:aws:s3:::open-learning-course-data-{app_env_suffix}",
            f"arn:aws:s3:::open-learning-course-data-{app_env_suffix}/*",
        ],
    },
    {
        "Action": ["s3:GetObject*", "s3:ListBucket*"],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production/*",
            "arn:aws:s3:::ol-olx-course-exports",
            "arn:aws:s3:::ol-olx-course-exports/*",
            "arn:aws:s3:::ocw-content-storage",
            "arn:aws:s3:::ocw-content-storage/*",
        ],
    },
]

athena_warehouse_access_statements = [
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
        "Resource": [
            f"arn:*:athena:*:*:workgroup/ol-warehouse-{stack_info.env_suffix}"
        ],
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
]
open_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": s3_bucket_permissions + athena_warehouse_access_statements,
}

mit_open_iam_policy = iam.Policy(
    f"mit_open_iam_permissions_{stack_info.env_suffix}",
    name=f"mit-open-application-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mit-open/{stack_info.env_suffix}/",
    policy=lint_iam_policy(open_policy_document, stringify=True),
)

mit_open_vault_iam_role = aws.SecretBackendRole(
    f"mit-open-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name=f"mit-open-application-{app_env_suffix}",
    # TODO: Make this configurable to support multiple AWS backends. TMM 2021-03-04
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[mit_open_iam_policy.arn],
)

export(
    "mit_open",
    {
        "iam_policy": mit_open_iam_policy.arn,
        "vault_iam_role": Output.all(
            mit_open_vault_iam_role.backend, mit_open_vault_iam_role.name
        ).apply(lambda role: f"{role[0]}/roles/{role[1]}"),
    },
)
