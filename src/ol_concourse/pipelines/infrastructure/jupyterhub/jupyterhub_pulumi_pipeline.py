from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

jupyterhub_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/jupyterhub/",
    ],
)

jupyter_pulumi_fragment = pulumi_jobs_chain(
    jupyterhub_pulumi_code,
    stack_names=[
        f"applications.jupyterhub.{stage}" for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-jupyterhub",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/jupyterhub/"),
)

combined_fragment = PipelineFragment(
    resource_types=jupyter_pulumi_fragment.resource_types,
    resources=jupyter_pulumi_fragment.resources,
    jobs=jupyter_pulumi_fragment.jobs,
)


jupyter_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        jupyterhub_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(jupyter_pipeline.model_dump_json(indent=2))
    sys.stdout.write(jupyter_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t <prod_target> sp -p pulumi-jupyterhub -c definition.json")  # noqa: T201
