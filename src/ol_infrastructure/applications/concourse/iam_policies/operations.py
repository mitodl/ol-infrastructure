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
                # learn-ai static frontend. The learn_ai Pulumi stack creates
                # ol-mit-learn-ai-{ci,qa,production}; the Fastly service in front
                # of them rewrites every request under the /frontend/ prefix, so
                # object writes are scoped to that prefix.
                "arn:aws:s3:::ol-mit-learn-ai-*",
                "arn:aws:s3:::ol-mit-learn-ai-*/frontend/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": "s3:ListBucketVersions",
            "Resource": "arn:aws:s3:::*",
        },
    ],
}
