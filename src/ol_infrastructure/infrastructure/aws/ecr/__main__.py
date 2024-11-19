import json

import pulumi_aws as aws
from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

ecr_config = Config("ecr")
stack_info = parse_stack()


ecr_policy = aws.iam.get_policy_document(
    statements=[
        {
            "sid": "ecr_repository_permissions",
            "effect": "Allow",
            "principals": [
                {
                    "type": "AWS",
                    "identifiers": [aws.get_caller_identity().account_id],
                }
            ],
            "actions": [
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:BatchCheckLayerAvailability",
                "ecr:PutImage",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload",
                "ecr:DescribeRepositories",
                "ecr:GetRepositoryPolicy",
                "ecr:ListImages",
                "ecr:DeleteRepository",
                "ecr:BatchDeleteImage",
                "ecr:SetRepositoryPolicy",
                "ecr:DeleteRepositoryPolicy",
            ],
        }
    ]
)
default_repository_creation_template = aws.ecr.RepositoryCreationTemplate(
    "aws-ecr-default-repository-template",
    description="Default template for ECR repositories",
    image_tag_mutability="MUTABLE",
    applied_fors=["PULL_THROUGH_CACHE"],
    repository_policy=ecr_policy.json,
    prefix="ROOT",
    resource_tags={
        "OU": "Operations",
    },
)

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
