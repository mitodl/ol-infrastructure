from pulumi import Config, StackReference, export
from pulumi_aws import athena, s3

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
    lifecycle_rules=s3.BucketLifecycleRuleArgs(
        enabled=True,
        expiration=s3.BucketLifecycleRuleExpirationArgs(days=30),
        id="expire_old_query_results",
    ),
)

athena_warehouse_workgroup = athena.Workgroup(
    f"ol_warehouse_athena_workgroup_{stack_info.env_suffix}",
    name=f"ol-warehouse-{stack_info.env_suffix}",
    description="Data warehousing for MIT Open Learning in the {stack_info.name} environment",
    state="ENABLED",
    tags=aws_config.merged_tags({"Name": f"ol-warehouse-{stack_info.env_suffix}"}),
    config=athena.WorkgroupConfigurationArgs(
        result_configuration=athena.WorkgroupConfigurationResultConfigurationArgs(
            encryption_configuration=athena.WorkgroupConfigurationResultConfigurationEncryptionConfigurationArgs(
                encryption_option="SSE_KMS",
                kms_key_arn=s3_kms_key["arn"],
            ),
            output_location=results_bucket.bucket.apply(
                lambda bucket_name: f"s3://{bucket_name}/output/"
            ),
            enforce_workgroup_configuration=True,
        ),
    ),
)

warehouse_buckets = []
warehouse_dbs = []
for unit in BusinessUnit:
    warehouse_buckets.append(
        s3.Bucket(
            f"ol_data_lake_s3_bucket_{unit.name}",
            bucket=f"ol-data-lake-{unit.value}-{stack_info.env_suffix}",
            server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(
                s3.BucketServerSideEncryptionConfigurationRuleArgs(
                    s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                        sse_algorithm="aws:kms",
                        kms_master_key_id=s3_kms_key["id"],
                    ),
                    bucket_key_enabled=True,
                )
            ),
            versioning=s3.BucketVersioningArgs(enabled=True),
            tags=aws_config.merged_tags({"OU": unit.value}),
        )
    )

    warehouse_dbs.append(
        athena.Database(
            f"ol_warehouse_database_{unit.name}_{stack_info.env_suffix}",
            name="ol_warehouse_{unit.name}_{stack_info.env_suffix}",
            encryption_configuration=athena.DatabaseEncryptionConfigurationArgs(
                encryption_option="SSE_KMS",
                kms_key=s3_kms_key["id"],
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
