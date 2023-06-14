from ol_concourse.pipelines.constants import PULUMI_CODE_PATH
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo

mit_open_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-mit_open"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_infrastructure/applications/mit_open/",
        "src/ol_infrastructure/lib/",
        "src/bridge/lib/",
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=mit_open_pulumi_code,
    stack_names=[
        "applications.mit_open.QA",
        "applications.mit_open.Production",
    ],
    project_name="ol-infrastructure-mit_open-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/mit_open/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
mit_open_pipeline = Pipeline(
    resources=[*mm_fragment.resources, mit_open_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(mit_open_pipeline.json(indent=2))
    sys.stdout.write(mit_open_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write("fly -t pr-inf set-pipeline -p pulumi-mit_open -c definition.json")
