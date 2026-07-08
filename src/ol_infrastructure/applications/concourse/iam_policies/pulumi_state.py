from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# Grants access to the shared Pulumi state backend bucket. Any worker pool that
# can run a pulumi_job() step needs this, since Concourse's built-in scheduler
# can place a team's build on an untagged/global worker pool (e.g. "generic")
# whenever that team's own workers are saturated.
policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetBucketLocation",
                "s3:GetObject*",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket*",
            ],
            "Resource": [
                "arn:aws:s3:::mitol-pulumi-state",
                "arn:aws:s3:::mitol-pulumi-state/*",
            ],
        },
    ],
}
