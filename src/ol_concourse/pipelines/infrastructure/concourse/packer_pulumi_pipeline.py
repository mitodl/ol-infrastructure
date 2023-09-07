import sys

from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, github_release
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

#############
# RESOURCES #
#############
concourse_release = github_release(
    Identifier("concourse-release"), "concourse", "concourse"
)
concourse_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/concourse",
        *PACKER_WATCHED_PATHS,
    ],
)

concourse_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/concourse",
        "src/bridge/secrets/concourse",
    ],
)

concourse_ami_fragment = packer_jobs(
    dependencies=[
        GetStep(
            get=concourse_release.name,
            trigger=True,
        )
    ],
    image_code=concourse_image_code,
    packer_template_path="src/bilder/images/.",
    node_types=["web", "worker"],
    packer_vars={"app_name": "concourse"},
    env_vars_from_files={"CONCOURSE_VERSION": f"{concourse_release.name}/version"},
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

concourse_pulumi_fragment = pulumi_jobs_chain(
    concourse_pulumi_code,
    stack_names=[
        f"applications.concourse.{stage}" for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-concourse-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/concourse/"),
    dependencies=[
        GetStep(
            get=concourse_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[concourse_ami_fragment.jobs[-1].name],
        )
    ],
)


combined_fragment = PipelineFragment(
    resource_types=concourse_ami_fragment.resource_types
    + concourse_pulumi_fragment.resource_types,
    resources=concourse_ami_fragment.resources + concourse_pulumi_fragment.resources,
    jobs=concourse_ami_fragment.jobs + concourse_pulumi_fragment.jobs,
)


def concourse_pipeline() -> Pipeline:
    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=[
            *combined_fragment.resources,
            concourse_release,
            concourse_image_code,
            concourse_pulumi_code,
        ],
        jobs=combined_fragment.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(concourse_pipeline().model_dump_json(indent=2))
    sys.stdout.write(concourse_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p packer-pulumi-concourse -c definition.json"
    )  # noqa: RUF100, T201
