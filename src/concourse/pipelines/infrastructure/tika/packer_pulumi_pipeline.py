from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from concourse.lib.resources import git_repo

# This job doesn't trigger automatically when a new version of Tika is released.
# tika_version will need to be updated in src/bilder/components/tika/models.py

tika_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/tika/",
        "src/bridge/lib/versions.py",
    ],
)

tika_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
)

tika_ami_fragment = packer_jobs(
    dependencies=[],
    image_code=tika_image_code,
    packer_template_path="src/bilder/images/",
    packer_vars={"app_name": "tika"},
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

tika_pulumi_fragment = pulumi_jobs_chain(
    tika_pulumi_code,
    project_name="ol-infrastructure-tika-server",
    stack_names=[f"applications.tika.{stage}" for stage in ("CI", "QA", "Production")],
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/tika/"),
    dependencies=[
        GetStep(
            get=tika_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[tika_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=tika_ami_fragment.resource_types
    + tika_pulumi_fragment.resource_types,
    resources=tika_ami_fragment.resources + tika_pulumi_fragment.resources,
    jobs=tika_ami_fragment.jobs + tika_pulumi_fragment.jobs,
)


tika_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources + [tika_image_code, tika_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys  # noqa: WPS433

    with open("definition.json", "wt") as definition:
        definition.write(tika_pipeline.json(indent=2))
    sys.stdout.write(tika_pipeline.json(indent=2))
    print()  # noqa: WPS421
    print("fly -t pr-inf sp -p packer-pulumi-tika -c definition.json")  # noqa: WPS421
