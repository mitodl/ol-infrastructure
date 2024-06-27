from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, github_release
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

vector_release = github_release(Identifier("vector-release"), "vectordotdev", "vector")

vector_log_proxy_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/vector_log_proxy/",
        "src/bridge/lib/versions.py",
        *PACKER_WATCHED_PATHS,
    ],
)

vector_log_proxy_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/infrastructure/vector_log_proxy",
    ],
)

get_vector_release = GetStep(get=vector_release.name, trigger=True)

vector_log_proxy_ami_fragment = packer_jobs(
    dependencies=[get_vector_release],
    image_code=vector_log_proxy_image_code,
    packer_template_path="src/bilder/images/",
    packer_vars={"app_name": "vector_log_proxy"},
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

vector_log_proxy_pulumi_fragment = pulumi_jobs_chain(
    vector_log_proxy_pulumi_code,
    stack_names=[
        f"infrastructure.vector_log_proxy.operations.{stage}"
        for stage in ("CI", "QA", "Production")
    ],
    project_name="ol-infrastructure-vector_log_proxy",
    project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/vector_log_proxy/"),
    dependencies=[
        GetStep(
            get=vector_log_proxy_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[vector_log_proxy_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=vector_log_proxy_ami_fragment.resource_types
    + vector_log_proxy_pulumi_fragment.resource_types,
    resources=vector_log_proxy_ami_fragment.resources
    + vector_log_proxy_pulumi_fragment.resources,
    jobs=vector_log_proxy_ami_fragment.jobs + vector_log_proxy_pulumi_fragment.jobs,
)


vector_log_proxy_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        vector_log_proxy_image_code,
        vector_release,
        vector_log_proxy_pulumi_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(vector_log_proxy_pipeline.model_dump_json(indent=2))
    sys.stdout.write(vector_log_proxy_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p packer-pulumi-vector-log-proxy -c definition.json"
    )  # noqa: RUF100, T201
