from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# S3 bucket permissions for publishing OCW
# S3 bucket permissions for uploading software artifacts
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
                "s3:*MultiPartUpload*",
                "s3:DeleteObject",
                "s3:GetObject*",
                "s3:ListBucket*",
                "s3:PutObject",
                "s3:PutObjectTagging",
            ],
            "Resource": [
                "arn:aws:s3:::ocw-content*",
                "arn:aws:s3:::ocw-content*/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:GetBucketVersioning",
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:ListBucket*",
            ],
            "Resource": [
                "arn:aws:s3:::ol-eng-artifacts",
                "arn:aws:s3:::ol-eng-artifacts/*",
                "arn:aws:s3:::ol-ocw-studio-app*",
                "arn:aws:s3:::ol-ocw-studio-app*/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject*", "s3:ListBucket"],
            "Resource": [
                "arn:aws:s3:::open-learning-course-data*",
                "arn:aws:s3:::open-learning-course-data*/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": "s3:ListBucketVersions",
            "Resource": "arn:aws:s3:::*",
        },
    ],
}
