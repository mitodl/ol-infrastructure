from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH

micromasters_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-micromasters"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_infrastructure/applications/micromasters/",
        "src/ol_infrastructure/lib/",
        "src/bridge/lib/",
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=micromasters_pulumi_code,
    stack_names=[
        "applications.micromasters.CI",
        "applications.micromasters.QA",
        "applications.micromasters.Production",
    ],
    project_name="ol-infrastructure-micromasters-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/micromasters/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
micromasters_pipeline = Pipeline(
    resources=[*mm_fragment.resources, micromasters_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(micromasters_pipeline.json(indent=2))
    sys.stdout.write(micromasters_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t pr-inf set-pipeline -p pulumi-micromasters -c definition.json"
    )
