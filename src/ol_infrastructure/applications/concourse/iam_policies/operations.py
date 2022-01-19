from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# These are default permissions a 'non-ocw' worker would require.
policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListAllMyBuckets",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:DeleteObject",
                "s3:ListBucket*",
            ],
            "Resource": [
                "arn:aws:s3:::*-edxapp-mfe",
                "arn:aws:s3:::*-edxapp-mfe/*",
                "arn:aws:s3:::ol-eng-artifacts",
                "arn:aws:s3:::ol-eng-artifacts/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": "s3:ListBucketVersions",
            "Resource": "arn:aws:s3:::*",
        },
    ],
}
