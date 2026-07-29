"""Build and publish the apisix-waf spike image (APISIX + Coraza proxy-wasm).

Deliberately NOT chained into a pulumi_jobs_chain -- this is an opt-in spike
image. `apisix_official.py` only references it when a per-cluster Pulumi
stack config explicitly sets `apisix_custom_image_repository`, so image
builds here never trigger a deploy on their own.
"""

import sys

from ol_concourse.lib.containers import container_build_task, ensure_ecr_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import ECR_REGION

ol_infrastructure_repo = git_repo(
    name=Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=["dockerfiles/apisix-waf/Dockerfile"],
)

ecr_image_resource = registry_image(
    name=Identifier("apisix-waf-image"),
    image_repository="mitodl/apisix-waf",
    ecr_region=ECR_REGION,
)


def build_job() -> Job:
    context = f"{ol_infrastructure_repo.name}/dockerfiles/apisix-waf"
    return Job(
        name=Identifier("build-and-publish"),
        public=True,
        plan=[
            GetStep(get=ol_infrastructure_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=ol_infrastructure_repo.name)],
                build_parameters={"CONTEXT": context},
            ),
            ensure_ecr_task("mitodl/apisix-waf"),
            PutStep(
                put=ecr_image_resource.name,
                inputs="detect",
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"{ol_infrastructure_repo.name}/.git/short_ref",
                },
            ),
        ],
    )


apisix_waf_pipeline = Pipeline(
    resources=[ol_infrastructure_repo, ecr_image_resource],
    jobs=[build_job()],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(apisix_waf_pipeline.model_dump_json(indent=2))
    sys.stdout.write(apisix_waf_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "\nfly -t pr-inf set-pipeline -p apisix-waf-docker -c definition.json\n"
    )
