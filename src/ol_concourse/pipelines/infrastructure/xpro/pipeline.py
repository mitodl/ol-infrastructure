from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH

xpro_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-xpro"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_infrastructure/applications/xpro/",
        "src/ol_infrastructure/lib/",
        "src/bridge/lib/",
        "src/bridge/secrets/xpro",
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=xpro_pulumi_code,
    stack_names=[
        "applications.xpro.CI",
        "applications.xpro.QA",
        "applications.xpro.Production",
    ],
    project_name="ol-infrastructure-xpro-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/xpro/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
xpro_pipeline = Pipeline(
    resources=[*mm_fragment.resources, xpro_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(xpro_pipeline.json(indent=2))
    sys.stdout.write(xpro_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write("fly -t pr-inf set-pipeline -p pulumi-xpro -c definition.json")
