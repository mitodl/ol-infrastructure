"""Follow-up ECR repository configuration, paired with ``ensure_ecr_task``.

``ensure_ecr_task`` (``ol_concourse.lib.containers``) only creates the
repository if it's missing, with AWS defaults: scanning disabled, unlimited
image retention. Pipelines whose ECR repo used to be a Pulumi-managed
``aws.ecr.Repository`` (scan-on-push + a lifecycle policy) but moved
ownership to the pipeline -- because a repo shared across CI/QA/Production
can't be owned by three independent per-env Pulumi stacks -- need this step
too, to keep the same repository configuration.
"""

import json

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Identifier,
    Platform,
    TaskConfig,
    TaskStep,
)

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri


def configure_ecr_repository_task(
    repo_name: str, *, keep_last_n_images: int = 10
) -> TaskStep:
    """Return a TaskStep applying scan-on-push + an image-count lifecycle policy.

    Safe to run on every pipeline execution: both
    ``put-image-scanning-configuration`` and ``put-lifecycle-policy`` are
    idempotent -- each call just (re)applies the same configuration.

    :param repo_name: The ECR repository name, e.g. ``"witan"``.
    :param keep_last_n_images: Expire all but the most recent N images.
    """
    lifecycle_policy = json.dumps(
        {
            "rules": [
                {
                    "rulePriority": 1,
                    "description": f"Keep last {keep_last_n_images} images",
                    "selection": {
                        "tagStatus": "any",
                        "countType": "imageCountMoreThan",
                        "countNumber": keep_last_n_images,
                    },
                    "action": {"type": "expire"},
                }
            ]
        }
    )
    return TaskStep(
        task=Identifier("configure-ecr-repository"),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=AnonymousResource(
                type="registry-image",
                source={
                    "repository": dockerhub_ecr_image_uri("amazon/aws-cli"),
                    "tag": "latest",
                    "aws_region": ECR_REGION,
                },
            ),
            params={
                "REPO_NAME": repo_name,
                "LIFECYCLE_POLICY": lifecycle_policy,
                "AWS_PAGER": "cat",
            },
            run=Command(
                path="sh",
                args=[
                    "-exc",
                    "aws ecr put-image-scanning-configuration"
                    " --repository-name ${REPO_NAME}"
                    " --image-scanning-configuration scanOnPush=true"
                    " && aws ecr put-lifecycle-policy"
                    " --repository-name ${REPO_NAME}"
                    ' --lifecycle-policy-text "$LIFECYCLE_POLICY"',
                ],
            ),
        ),
    )
