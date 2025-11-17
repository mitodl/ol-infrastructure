from ol_concourse.lib.jobs.infrastructure import (
    pulumi_jobs_chain,  # noqa: D100, INP001, RUF100
)
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

digital_credentials_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/digital_credentials/",
        "src/bridge/secrets/digital_credentials/",
    ],
)

digital_credentials_pulumi_fragment = pulumi_jobs_chain(
    digital_credentials_pulumi_code,
    stack_names=[
        f"applications.digital_credentials.{stage}"
        for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-open-metadata-server",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/digital_credentials/"),
)

combined_fragment = PipelineFragment(
    resource_types=digital_credentials_pulumi_fragment.resource_types,
    resources=digital_credentials_pulumi_fragment.resources,
    jobs=digital_credentials_pulumi_fragment.jobs,
)


digital_credentials_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        digital_credentials_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(digital_credentials_pipeline.model_dump_json(indent=2))
    sys.stdout.write(digital_credentials_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-digital-credentials -c definition.json")  # noqa: T201
