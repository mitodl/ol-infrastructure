from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from concourse.lib.models.pipeline import Identifier, Pipeline
from concourse.lib.resources import git_repo

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

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=ocw_site_pulumi_code,
    stack_names=[
        "applications.ocw_site.QA",
        "applications.ocw_site.Production",
    ],
    project_name="ol-infrastructure-ocw-site-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/ocw_site/"),
)

ocw_site_pipeline = Pipeline(
    resources=pulumi_jobs.resources + [ocw_site_pulumi_code],
    resource_types=pulumi_jobs.resource_types,
    jobs=pulumi_jobs.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "wt") as definition:
        definition.write(ocw_site_pipeline.json(indent=2))
    sys.stdout.write(ocw_site_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write("fly -t pr-inf set-pipeline -p pulumi-ocw-site -c definition.json")
