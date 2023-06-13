from concourse.pipelines.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from concourse.lib.models.pipeline import Identifier, Pipeline
from concourse.lib.resources import git_repo

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

micromasters_pipeline = Pipeline(
    resources=[*pulumi_jobs.resources, micromasters_pulumi_code],
    resource_types=pulumi_jobs.resource_types,
    jobs=pulumi_jobs.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(micromasters_pipeline.json(indent=2))
    sys.stdout.write(micromasters_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t pr-inf set-pipeline -p pulumi-micromasters -c definition.json"
    )
