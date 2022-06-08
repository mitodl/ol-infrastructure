import json
from typing import Any, Dict, List, Union

from pulumi import Config, StackReference, export
from pulumi_aws import athena, glue, iam, s3

from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit
from ol_infrastructure.lib.pulumi_helper import parse_stack

data_warehouse_config = Config("data_warehouse")
stack_info = parse_stack()
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
aws_config = AWSBase(
    tags={
        "OU": "data",
        "Environment": f"data-{stack_info.env_suffix}",
        "Owner": "platform-engineering",
        "Application": "data-warehouse",
    },
)
s3_kms_key = kms_stack.require_output("kms_s3_data_analytics_key")

results_bucket = s3.Bucket(
    f"ol_warehouse_results_bucket_{stack_info.env_suffix}",
    bucket=f"ol-warehouse-results-{stack_info.env_suffix}",
    acl="private",
    server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(
        rule=s3.BucketServerSideEncryptionConfigurationRuleArgs(
            apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                sse_algorithm="aws:kms",
                kms_master_key_id=s3_kms_key["id"],
            ),
            bucket_key_enabled=True,
        )
    ),
    tags=aws_config.tags,
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            expiration=s3.BucketLifecycleRuleExpirationArgs(days=30),
            id="expire_old_query_results",
        )
    ],
)
s3.BucketPublicAccessBlock(
    f"ol_warehouse_results_bucket_{stack_info.env_suffix}_block_public_access",
    bucket=results_bucket.bucket,
    block_public_acls=True,
    block_public_policy=True,
)

athena_warehouse_workgroup = athena.Workgroup(
    f"ol_warehouse_athena_workgroup_{stack_info.env_suffix}",
    name=f"ol-warehouse-{stack_info.env_suffix}",
    description="Data warehousing for MIT Open Learning in the "
    f"{stack_info.name} environment",
    state="ENABLED",
    tags=aws_config.merged_tags({"Name": f"ol-warehouse-{stack_info.env_suffix}"}),
    configuration=athena.WorkgroupConfigurationArgs(
        result_configuration=athena.WorkgroupConfigurationResultConfigurationArgs(
            encryption_configuration=athena.WorkgroupConfigurationResultConfigurationEncryptionConfigurationArgs(
                encryption_option="SSE_KMS",
                kms_key_arn=s3_kms_key["arn"],
            ),
            output_location=results_bucket.bucket.apply(
                lambda bucket_name: f"s3://{bucket_name}/output/"
            ),
        ),
        enforce_workgroup_configuration=True,
    ),
)

warehouse_buckets = []
warehouse_dbs = []
for unit in BusinessUnit:
    lake_storage_bucket = s3.Bucket(
        f"ol_data_lake_s3_bucket_{unit.name}_{stack_info.env_suffix}",
        bucket=f"ol-data-lake-{unit.value}-{stack_info.env_suffix}",
        acl="private",
        server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(
            rule=s3.BucketServerSideEncryptionConfigurationRuleArgs(
                apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                    sse_algorithm="aws:kms",
                    kms_master_key_id=s3_kms_key["id"],
                ),
                bucket_key_enabled=True,
            )
        ),
        versioning=s3.BucketVersioningArgs(enabled=True),
        tags=aws_config.merged_tags({"OU": unit.value}),
    )
    warehouse_buckets.append(lake_storage_bucket)
    s3.BucketPublicAccessBlock(
        f"ol_data_lake_s3_bucket_{unit.name}_{stack_info.env_suffix}_block_public_access",
        bucket=lake_storage_bucket.bucket,
        block_public_acls=True,
        block_public_policy=True,
    )

    warehouse_dbs.append(
        glue.CatalogDatabase(
            f"ol_warehouse_database_{unit.name}_{stack_info.env_suffix}",
            name=f"ol_warehouse_{unit.name}_{stack_info.env_suffix}",
            description=f"Data mart for information owned by or sourced from {unit} in the {stack_info.env_suffix} environment.",
            location_uri=lake_storage_bucket.bucket.apply(
                lambda bucket: f"s3://{bucket}"
            ),
        )
    )

export(
    "athena_data_warehouse",
    {
        "source_buckets": [bucket.bucket for bucket in warehouse_buckets],
        "results_bucket": results_bucket.bucket,
        "databases": [database.name for database in warehouse_dbs],
        "workgroup": athena_warehouse_workgroup.name,
    },
)

parliament_config: Dict[str, Any] = {
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []}
}

query_engine_permissions: List[Dict[str, Union[str, List[str]]]] = [
    {
        "Effect": "Allow",
        "Action": [
            "glue:TagResource",
            "glue:UnTagResource",
        ],
        "Resource": ["*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "glue:BatchCreatePartition",
            "glue:BatchDeletePartition",
            "glue:BatchDeleteTable",
            "glue:BatchGetPartition",
            "glue:CreateTable",
            "glue:CreatePartition",
            "glue:DeletePartition",
            "glue:DeleteTable",
            "glue:GetDatabase",
            "glue:GetDatabases",
            "glue:GetPartition",
            "glue:GetPartitions",
            "glue:GetTable",
            "glue:GetTables",
            "glue:UpdateDatabase",
            "glue:UpdatePartition",
            "glue:UpdateTable",
        ],
        "Resource": [
            "arn:aws:glue:*:*:catalog",
            f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:PutObject",
            "s3:GetObject",
            "s3:ListBucketMultipartUploads",
            "s3:ListBucketVersions",
            "s3:ListBucket",
            "s3:DeleteObject",
            "s3:GetObjectVersion",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-data-lake-data-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-data-{stack_info.env_suffix}/*",
        ],
    },
]

query_engine_iam_permissions = {
    "Version": "2012-10-17",
    "Statement": query_engine_permissions,
}

# Create instance profile for granting access to S3 buckets
query_engine_iam_policy = iam.Policy(
    f"data-lake-query-engine-policy-{stack_info.env_suffix}",
    name=f"query-engine-policy-{stack_info.env_suffix}",
    path=f"/ol-data/etl-policy-{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        query_engine_iam_permissions,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="Policy for granting access to Glue and S3 to query engine",
)

query_engine_role = iam.Role(
    "query-engine-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name=f"data-lake-query-engine-policy-{stack_info.env_suffix}",
    path="/ol-data/etl-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"query-engine-role-policy-{stack_info.env_suffix}",
    policy_arn=query_engine_iam_policy.arn,
    role=query_engine_role.name,
)
