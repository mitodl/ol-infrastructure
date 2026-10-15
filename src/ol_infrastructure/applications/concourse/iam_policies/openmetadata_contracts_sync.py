from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# The ol-data-contracts-sync pipeline (ol_concourse/pipelines/applications/
# ol_data_contracts_sync.py) resolves ol-data-platform's dbt-bound data contracts
# with the production manifest Dagster uploads nightly for OpenMetadata. Listed
# only on the Production workers an infrastructure-team build can land on (infra
# and generic), since that pipeline is only set on the Production Concourse.
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
