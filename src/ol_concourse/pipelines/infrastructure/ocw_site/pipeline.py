from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH

ocw_site_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_infrastructure/applications/ocw_site/",
        "src/ol_infrastructure/lib/",
        "src/bridge/lib/",
    ],
)

ocw_theme_code = git_repo(
    name=Identifier("ocw-hugo-theme"),
    uri="https://github.com/mitodl/ocw-hugo-themes",
    branch="release",
    paths=["www/layouts/404.html"],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=ocw_site_pulumi_code,
    stack_names=[
        "applications.ocw_site.QA",
        "applications.ocw_site.Production",
    ],
    project_name="ol-infrastructure-ocw-site-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/ocw_site/"),
    dependencies=[GetStep(get=ocw_theme_code.name, trigger=True)],
)

fragment = PipelineFragment.combine_fragments(pulumi_jobs)

ocw_site_pipeline = Pipeline(
    resources=[*fragment.resources, ocw_site_pulumi_code, ocw_theme_code],
    resource_types=fragment.resource_types,
    jobs=fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(ocw_site_pipeline.model_dump_json(indent=2))
    sys.stdout.write(ocw_site_pipeline.model_dump_json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write("fly -t pr-inf set-pipeline -p pulumi-ocw-site -c definition.json")
