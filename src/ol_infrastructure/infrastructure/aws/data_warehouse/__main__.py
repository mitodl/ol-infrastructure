import json
from typing import Any

from pulumi import Config, StackReference, export
from pulumi_aws import athena, glue, iam, s3

from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

current_aws_account = s3.get_canonical_user_id()
data_warehouse_config = Config("data_warehouse")
data_lake_query_engine_config = Config("data-lake-query-engine")
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

data_stages = ("raw", "staging", "intermediate", "mart", "external")

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
    description=(
        f"Data warehousing for MIT Open Learning in the {stack_info.name} environment"
    ),
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
data_landing_zone_bucket = s3.Bucket(
    "ol_data_lake_landing_zone_bucket",
    bucket=f"ol-data-lake-landing-zone-{stack_info.env_suffix}",
)
data_landing_zone_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol_data_lake_landing_zone_bucket_ownership_controls",
    bucket=data_landing_zone_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketServerSideEncryptionConfiguration(
    "encrypt_ol_data_lake_landing_zone_bucket",
    bucket=data_landing_zone_bucket.id,
    rules=[
        s3.BucketServerSideEncryptionConfigurationRuleArgs(
            bucket_key_enabled=True,
            apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                sse_algorithm="aws:kms"
            ),
        )
    ],
)
warehouse_buckets.append(data_landing_zone_bucket)
warehouse_dbs = []
for data_stage in data_stages:
    lake_storage_bucket = s3.Bucket(
        f"ol_data_lake_s3_bucket_{data_stage}",
        bucket=f"ol-data-lake-{data_stage}-{stack_info.env_suffix}",
        acl="private",
        server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(
            rule=s3.BucketServerSideEncryptionConfigurationRuleArgs(
                apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                    sse_algorithm="aws:kms",
                    kms_master_key_id=s3_kms_key["arn"],
                ),
                bucket_key_enabled=True,
            )
        ),
        versioning=s3.BucketVersioningArgs(enabled=True),
        tags=aws_config.merged_tags({"OU": "data"}),
    )
    warehouse_buckets.append(lake_storage_bucket)
    s3.BucketPublicAccessBlock(
        f"ol_data_lake_s3_bucket_{data_stage}_block_public_access",
        bucket=lake_storage_bucket.bucket,
        block_public_acls=True,
        block_public_policy=True,
    )
    warehouse_db = glue.CatalogDatabase(
        f"ol_warehouse_database_{data_stage}",
        name=f"ol_warehouse_{stack_info.env_suffix}_{data_stage}",
        description=(
            f"Data mart for data in {data_stage} format in the"
            f" {stack_info.env_suffix} environment."
        ),
        location_uri=lake_storage_bucket.bucket.apply(lambda bucket: f"s3://{bucket}/"),
    )
    warehouse_dbs.append(warehouse_db)

export(
    "data_warehouse",
    {
        "source_buckets": [bucket.bucket for bucket in warehouse_buckets],
        "results_bucket": results_bucket.bucket,
        "databases": [database.name for database in warehouse_dbs],
        "workgroup": athena_warehouse_workgroup.name,
    },
)

parliament_config: dict[str, Any] = {
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []}
}

query_engine_permissions: list[dict[str, str | list[str]]] = [
    {
        "Effect": "Allow",
        "Action": [
            "glue:TagResource",
            "glue:UnTagResource",
            "s3:ListAllMyBuckets",
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
            "glue:BatchUpdatePartition",
            "glue:CreateDatabase",
            "glue:CreatePartition",
            "glue:CreateTable",
            "glue:DeleteColumnStatisticsForPartition",
            "glue:DeleteColumnStatisticsForTable",
            "glue:DeleteDatabase",
            "glue:DeletePartition",
            "glue:DeleteTable",
            "glue:GetColumnStatisticsForPartition",
            "glue:GetColumnStatisticsForTable",
            "glue:GetDatabase",
            "glue:GetDatabases",
            "glue:GetPartition",
            "glue:GetPartitions",
            "glue:GetTable",
            "glue:GetTables",
            "glue:UpdateColumnStatisticsForPartition",
            "glue:UpdateColumnStatisticsForTable",
            "glue:UpdateDatabase",
            "glue:UpdatePartition",
            "glue:UpdateTable",
        ],
        "Resource": [
            "arn:aws:glue:*:*:catalog",
            "arn:aws:glue:*:*:database/information_schema",
            f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}*",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}*/*",
            f"arn:aws:glue:*:*:userDefinedFunction/*{stack_info.env_suffix}*/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetBucketPolicy",
            "s3:GetObject",
            "s3:GetObjectAttributes",
            "s3:GetObjectVersion",
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
            "s3:ListBucketVersions",
            "s3:PutObject",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-data-lake-{stage}-{stack_info.env_suffix}"
            for stage in data_stages
        ]
        + [
            f"arn:aws:s3:::ol-data-lake-{stage}-{stack_info.env_suffix}/*"
            for stage in data_stages
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
    name=f"data-lake-query-engine-policy-{stack_info.env_suffix}",
    path=f"/ol-data/etl-policy-{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        query_engine_iam_permissions,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="Policy for granting access to Glue and S3 to data lake query engine",
)

query_engine_aws_account_id = data_lake_query_engine_config.require("aws-account-id")
query_engine_aws_external_id = data_lake_query_engine_config.require("aws-external-id")

query_engine_role = iam.Role(
    "data-lake-query-engine-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {
                    "AWS": f"arn:aws:iam::{query_engine_aws_account_id}:root"
                },
                "Condition": {
                    "StringEquals": {"sts:ExternalId": query_engine_aws_external_id}
                },
            },
        }
    ),
    name=f"data-lake-query-engine-role-{stack_info.env_suffix}",
    path="/ol-data/etl-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"data-lake-query-engine-role-policy-{stack_info.env_suffix}",
    policy_arn=query_engine_iam_policy.arn,
    role=query_engine_role.name,
)

export("sql_engine_role_arn", query_engine_role.arn)
