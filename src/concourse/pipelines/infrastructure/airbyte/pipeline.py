from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from concourse.lib.resources import git_repo, github_release

airbyte_release = github_release(Identifier("airbyte-release"), "airbytehq", "airbyte")
airbyte_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/airbyte/",
    ],
)

airbyte_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/applications/airbyte/",
        "pipelines/infrastructure/scripts/",
    ],
)

get_airbyte_release = GetStep(get=airbyte_release.name, trigger=True)

airbyte_ami_fragment = packer_jobs(
    dependencies=[get_airbyte_release],
    image_code=airbyte_image_code,
    packer_template_path="src/bilder/images/airbyte/airbyte.pkr.hcl",
    env_vars_from_files={"AIRBYTE_VERSION": "airbyte-release/version"},
)

airbyte_pulumi_fragment = pulumi_jobs_chain(
    airbyte_pulumi_code,
    # stack_name="applications.airbyte.QA",
    stack_names=[f"applications.airbyte.{stage}" for stage in ("QA", "Production")],
    project_name="ol-infrastructure-airbyte-server",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/airbyte/"),
    dependencies=[
        GetStep(
            get=airbyte_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[airbyte_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=airbyte_ami_fragment.resource_types
    + airbyte_pulumi_fragment.resource_types,
    resources=airbyte_ami_fragment.resources + airbyte_pulumi_fragment.resources,
    jobs=airbyte_ami_fragment.jobs + airbyte_pulumi_fragment.jobs,
)


airbyte_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources
    + [airbyte_image_code, airbyte_release, airbyte_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "wt") as definition:
        definition.write(airbyte_pipeline.json(indent=2))
    sys.stdout.write(airbyte_pipeline.json(indent=2))
