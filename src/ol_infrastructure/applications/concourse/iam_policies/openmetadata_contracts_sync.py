from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# The ol-data-contracts-sync, ol-data-metrics-sync and ol-data-glossary-sync
# pipelines (ol_concourse/pipelines/applications/ol_data_contracts_sync.py,
# ol_data_metrics_sync.py and ol_data_glossary_sync.py) download the production
# manifest Dagster uploads nightly for OpenMetadata, which resolves
# ol-data-platform's dbt-bound data contracts and metrics. Listed only on the
# Production workers an infrastructure-team build can land on (infra and
# generic), since those pipelines are only set on the Production Concourse.
policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": (
                "arn:aws:s3:::dagster-data-production/openmetadata/dbt-artifacts/manifest.json"
            ),
        },
    ],
}
