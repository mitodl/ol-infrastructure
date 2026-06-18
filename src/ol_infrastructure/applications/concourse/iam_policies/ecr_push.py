from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# AWS Permissions Document
# Allow Concourse workers to push images to ECR repositories under mitodl/*.
policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "ecr:GetAuthorizationToken",
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "ecr:BatchCheckLayerAvailability",
                "ecr:CompleteLayerUpload",
                "ecr:InitiateLayerUpload",
                "ecr:PutImage",
                "ecr:UploadLayerPart",
            ],
            "Resource": "arn:aws:ecr:*:*:repository/mitodl/*",
        },
    ],
}
