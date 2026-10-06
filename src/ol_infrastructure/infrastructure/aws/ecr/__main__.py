import json

import pulumi_aws as aws
from pulumi import Config

from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.pulumi_helper import parse_stack

ecr_config = Config("ecr")
stack_info = parse_stack()
aws_account_id = aws.get_caller_identity().account_id

ecr_policy = aws.iam.get_policy_document(
    statements=[
        {
            "sid": "ecr_repository_permissions",
            "effect": "Allow",
            "principals": [
                {
                    "type": "AWS",
                    "identifiers": [aws_account_id],
                }
            ],
            "actions": [
                "ecr:BatchCheckLayerAvailability",
                "ecr:BatchDeleteImage",
                "ecr:BatchGetImage",
                "ecr:CompleteLayerUpload",
                "ecr:DeleteRepository",
                "ecr:DeleteRepositoryPolicy",
                "ecr:DescribeImageScanFindings",
                "ecr:DescribeImages",
                "ecr:DescribeRepositories",
                "ecr:GetAuthorizationToken",
                "ecr:GetDownloadUrlForLayer",
                "ecr:GetLifecyclePolicy",
                "ecr:GetLifecyclePolicyPreview",
                "ecr:GetRepositoryPolicy",
                "ecr:InitiateLayerUpload",
                "ecr:ListImages",
                "ecr:ListTagsForResource",
                "ecr:PutImage",
                "ecr:SetRepositoryPolicy",
                "ecr:UploadLayerPart",
            ],
        }
    ]
)

# Cached images are deployed by digest, so nearly all are untagged and an
# "expire untagged" rule would delete live images. Expiry by push age is safe for
# a cache: a miss re-fetches from upstream. ECR's sinceImagePulled can only
# archive, and archived images cannot be pulled.
pull_through_cache_lifecycle_policy = json.dumps(
    {
        "rules": [
            {
                "rulePriority": 1,
                "description": "Expire cached images pushed more than 90 days ago",
                "selection": {
                    "tagStatus": "any",
                    "countType": "sinceImagePushed",
                    "countUnit": "days",
                    "countNumber": 90,
                },
                "action": {"type": "expire"},
            }
        ]
    }
)

default_repository_creation_template = aws.ecr.RepositoryCreationTemplate(
    "aws-ecr-default-repository-template",
    description="Default template for ECR repositories",
    image_tag_mutability="MUTABLE",
    applied_fors=["PULL_THROUGH_CACHE"],
    repository_policy=ecr_policy.json,
    lifecycle_policy=pull_through_cache_lifecycle_policy,
    prefix="ROOT",
)

ecr_registry_policy = aws.ecr.RegistryPolicy(
    "ecr-us-east-registry-policy",
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": f"arn:aws:iam::{aws_account_id}:root",
                    },
                    "Action": ["ecr:CreateRepository", "ecr:BatchImportUpstreamImage"],
                    "Resource": [
                        f"arn:aws:ecr:{aws.get_region().name}:{aws_account_id}:repository/*"
                    ],
                }
            ],
        }
    ),
)


pull_through_cache_prefixes = ("dockerhub/", "ecr-public/")

aws.ecr.PullThroughCacheRule(
    "aws-ecr-public-pull-through-cache-rule",
    upstream_registry_url="public.ecr.aws",
    ecr_repository_prefix="ecr-public",
)


dockerhub_credential = aws.secretsmanager.Secret(
    "dockerhub-credentials",
    name_prefix="ecr-pullthroughcache/",
    description="Docker Hub credentials for ECR pull through cache",
)


dockerhub_credential_value = aws.secretsmanager.SecretVersion(
    "dockerhub-credentials-value",
    secret_id=dockerhub_credential.id,
    secret_string=json.dumps(
        {
            "username": ecr_config.require("dockerhub_username"),
            "accessToken": ecr_config.require("dockerhub_password"),
        }
    ),
)

aws.ecr.PullThroughCacheRule(
    "aws-ecr-dockerhub-pull-through-cache-rule",
    upstream_registry_url="registry-1.docker.io",
    ecr_repository_prefix="dockerhub",
    credential_arn=dockerhub_credential.arn,
)

# The creation template only applies to repositories created after it changes, so
# the cache repositories that already exist get the policy attached directly.
for repository_name in aws.ecr.get_repositories().names:
    if repository_name.startswith(pull_through_cache_prefixes):
        aws.ecr.LifecyclePolicy(
            f"ecr-pull-through-cache-lifecycle-{repository_name.replace('/', '-')}",
            repository=repository_name,
            policy=pull_through_cache_lifecycle_policy,
        )

ecr_private_repository = aws.ecr.Repository(
    "aws-ecr-private-repository", name="ol-course-notebooks"
)

ecr_private_repository_policy = aws.ecr.RepositoryPolicy(
    "aws-ecr-private-repository-policy",
    repository=ecr_private_repository.name,
    policy=ecr_policy.json,
)
