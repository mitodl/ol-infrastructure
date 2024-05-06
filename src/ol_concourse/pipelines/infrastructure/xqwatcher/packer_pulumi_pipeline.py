from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

xqwatcher_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/xqwatcher/",
        "src/bridge/lib/versions.py",
        *PACKER_WATCHED_PATHS,
    ],
)

xqwatcher_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        PULUMI_CODE_PATH.joinpath("applications/xqwatcher/"),
    ],
)

xqwatcher_ami_fragment = packer_jobs(
    dependencies=[],
    image_code=xqwatcher_image_code,
    packer_template_path="src/bilder/images/xqwatcher/xqwatcher.pkr.hcl",
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.xqwatcher"]},
)

xqwatcher_pulumi_fragment = pulumi_jobs_chain(
    xqwatcher_pulumi_code,
    project_name="ol-infrastructure-xqwatcher-server",
    stack_names=[
        f"applications.xqwatcher.{stage}" for stage in ("CI", "QA", "Production")
    ],
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/xqwatcher/"),
    dependencies=[
        GetStep(
            get=xqwatcher_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[xqwatcher_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=xqwatcher_ami_fragment.resource_types
    + xqwatcher_pulumi_fragment.resource_types,
    resources=xqwatcher_ami_fragment.resources + xqwatcher_pulumi_fragment.resources,
    jobs=xqwatcher_ami_fragment.jobs + xqwatcher_pulumi_fragment.jobs,
)


xqwatcher_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        xqwatcher_image_code,
        xqwatcher_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(xqwatcher_pipeline.model_dump_json(indent=2))
    sys.stdout.write(xqwatcher_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-pulumi-xqwatcher -c definition.json")  # noqa: T201
