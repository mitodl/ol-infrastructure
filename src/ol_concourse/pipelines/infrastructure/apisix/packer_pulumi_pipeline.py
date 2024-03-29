from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

apisix_docker_image = registry_image(
    name=Identifier("apisix-docker-image"),
    image_repository="apache/apisix",
)

apisix_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/apisix/",
        "src/bridge/lib/versions.py",
        *PACKER_WATCHED_PATHS,
    ],
)

apisix_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        PULUMI_CODE_PATH.joinpath("applications/apisix/"),
    ],
)

apisix_ami_fragment = packer_jobs(
    dependencies=[GetStep(get=apisix_docker_image.name, trigger=True)],
    image_code=apisix_image_code,
    packer_template_path="src/bilder/images/apisix/apisix.pkr.hcl",
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.apisix"]},
)

apisix_pulumi_fragment = pulumi_jobs_chain(
    apisix_pulumi_code,
    project_name="ol-infrastructure-apisix-server",
    stack_names=[
        f"applications.apisix.{stage}" for stage in ("CI", "QA", "Production")
    ],
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/apisix/"),
    dependencies=[
        GetStep(
            get=apisix_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[apisix_ami_fragment.jobs[-1].name],
        )
    ],
)

combined_fragment = PipelineFragment(
    resource_types=apisix_ami_fragment.resource_types
    + apisix_pulumi_fragment.resource_types,
    resources=apisix_ami_fragment.resources + apisix_pulumi_fragment.resources,
    jobs=apisix_ami_fragment.jobs + apisix_pulumi_fragment.jobs,
)


apisix_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        apisix_image_code,
        apisix_pulumi_code,
        apisix_docker_image,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(apisix_pipeline.model_dump_json(indent=2))
    sys.stdout.write(apisix_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-pulumi-apisix -c definition.json")  # noqa: T201
