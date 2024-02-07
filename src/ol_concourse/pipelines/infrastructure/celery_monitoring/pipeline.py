from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH

celery_monitoring_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-celery-monitoring"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=[
        "src/ol_infrastructure/applications/celery_monitoring/",
        "src/ol_infrastructure/lib/",
        "src/bridge/lib/",
    ],
)

pulumi_jobs = pulumi_jobs_chain(
    pulumi_code=celery_monitoring_pulumi_code,
    stack_names=[
        "applications.celery_monitoring.CI",
        "applications.celery_monitoring.QA",
        "applications.celery_monitoring.Production",
    ],
    project_name="ol-infrastructure-celery-monitoring-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/celery_monitoring/"),
)

mm_fragment = PipelineFragment.combine_fragments(pulumi_jobs)
celery_monitoring_pipeline = Pipeline(
    resources=[*mm_fragment.resources, celery_monitoring_pulumi_code],
    resource_types=mm_fragment.resource_types,
    jobs=mm_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(celery_monitoring_pipeline.json(indent=2))
    sys.stdout.write(celery_monitoring_pipeline.json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t pr-inf set-pipeline -p pulumi-celery-monitoring -c definition.json"
    )
