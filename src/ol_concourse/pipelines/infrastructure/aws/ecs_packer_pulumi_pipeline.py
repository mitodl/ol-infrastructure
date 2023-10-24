import sys

from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

ecs_packer_code_trigger = git_repo(
    Identifier("ol-infrastructure-packer-trigger"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    check_every="1h",
    paths=[
        "src/bilder/components",
        "src/bilder/images/ecs",
        *PACKER_WATCHED_PATHS,
    ],
)

ecs_pulumi_code_trigger = git_repo(
    Identifier("ol-infrastructure-pulumi-trigger"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    check_every="1h",
    paths=[
        "src/ol_infrastructure/infrastructure/aws/ecs",
        *PULUMI_WATCHED_PATHS,
    ],
)

ol_infrastructure_code = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    check_every="never",
)

ecs_packer_fragment = packer_jobs(
    dependencies=[
        GetStep(
            get=ecs_packer_code_trigger.name,
            trigger=True,
        ),
    ],
    image_code=ol_infrastructure_code,
    packer_template_path="src/bilder/images/ecs/.",
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.ecs"]},
)

# Make stack_names smarter when there are more stacks
ecs_pulumi_fragment = pulumi_jobs_chain(
    ol_infrastructure_code,
    stack_names=["infrastructure.aws.ecs.data.CI"],
    project_name="ol-infrastructure-ecs-infrastructure",
    project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/aws/concourse/"),
    dependencies=[
        GetStep(
            get=ecs_pulumi_code_trigger.name,
            trigger=True,
        ),
        GetStep(
            get=ecs_packer_fragment.resources[-1].name,
            trigger=True,
            passed=[ecs_packer_fragment.jobs[-1].name],
        ),
    ],
)

combined_fragment = PipelineFragment(
    resource_types=ecs_packer_fragment.resource_types
    + ecs_pulumi_fragment.resource_types,
    resources=ecs_packer_fragment.resources + ecs_pulumi_fragment.resources,
    jobs=ecs_packer_fragment.jobs + ecs_pulumi_fragment.jobs,
)


def ecs_pipeline() -> Pipeline:
    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=[
            *combined_fragment.resources,
            ol_infrastructure_code,
            ecs_packer_code_trigger,
            ecs_pulumi_code_trigger,
        ],
        jobs=combined_fragment.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(ecs_pipeline().model_dump_json(indent=2))
    sys.stdout.write(ecs_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p packer-pulumi-ecs -c definition.json"
    )  # noqa: RUF100, T201
