import sys
from pathlib import Path
from typing import Literal, TypedDict

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import git_repo, github_release, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

EnvironmentType = Literal["ci", "qa", "prod"]


class PipelineConfigDict(TypedDict):
    branch: str
    image_name: str
    packer_build: str
    environment: Literal["ci", "qa", "prod"]
    stacks: list[str]


PIPELINE_CONFIG: dict[Literal["main", "ci"], PipelineConfigDict] = {
    "main": {
        "branch": "main",
        "image_name": "superset",
        "packer_build": "amazon-ebs.superset",
        "environment": "prod",
        "stacks": ["applications.superset.QA", "applications.superset.Production"],
    },
    "ci": {
        "branch": "ci",
        "image_name": "superset-ci",
        "packer_build": "amazon-ebs.superset-ci",
        "environment": "ci",
        "stacks": ["applications.superset.CI"],
    },
}


class ResourceConfigError(TypeError):
    """Base exception for resource configuration errors."""


class InvalidBranchTypeError(ResourceConfigError):
    def __init__(self, got_type: str) -> None:
        super().__init__(f"Branch must be a string, got {got_type}")


class InvalidEnvironmentError(ValueError):
    def __init__(self, environment: str) -> None:
        super().__init__(f"Invalid environment: {environment}")


def create_base_resources(
    branch: str, environment: EnvironmentType = "prod"
) -> tuple[Resource, Resource, Resource]:
    """Create base resources with branch-specific config

    Args:
        branch: Git branch to use (must be string)
        environment: Environment type (ci/qa/prod)

    Returns:
        Tuple of (superset_release, docker_code_repo, superset_image) resources

    Raises:
        InvalidBranchTypeError: If branch is not a string
        InvalidEnvironmentError: If environment is invalid
    """
    if not isinstance(branch, str):
        raise InvalidBranchTypeError(type(branch).__name__)

    if environment not in ("ci", "qa", "prod"):
        raise InvalidEnvironmentError(environment)

    superset_release = github_release(
        name=Identifier("superset-release"),
        owner="apache",
        repository="superset",
        tag_filter="^4",
        order_by="time",
    )

    docker_code_repo = git_repo(
        Identifier("ol-inf-superset-docker-code"),
        uri="https://github.com/mitodl/ol_infrastructure",
        branch=branch,
        paths=["src/ol_superset/"],
    )

    image_suffix = {"ci": "-ci", "qa": "-qa", "prod": ""}[environment]

    image_name = f"superset{image_suffix}"
    superset_image = registry_image(
        name=Identifier(f"superset-image{image_suffix}"),
        image_repository=f"mitodl/{image_name}",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    return superset_release, docker_code_repo, superset_image


def create_pipeline_fragment(
    docker_build_job: Job,
    packer_fragment: PipelineFragment,
    pulumi_fragment: PipelineFragment,
    resources: list[Resource],
) -> PipelineFragment:
    """Create combined pipeline fragment from components"""
    return PipelineFragment(
        resource_types=packer_fragment.resource_types + pulumi_fragment.resource_types,
        resources=[*resources, *packer_fragment.resources, *pulumi_fragment.resources],
        jobs=[docker_build_job, *packer_fragment.jobs, *pulumi_fragment.jobs],
    )


def create_build_job(
    name: str,
    superset_release: Resource,
    docker_code_repo: Resource,
    superset_image: Resource,
) -> Job:
    """Create standardized build job"""
    return Job(
        name=name,
        plan=[
            GetStep(get=superset_release.name, trigger=True),
            GetStep(get=docker_code_repo.name, trigger=True),
            LoadVarStep(
                load_var="superset_tag",
                reveal=True,
                file=f"{superset_release.name}/tag",
            ),
            container_build_task(
                inputs=[Input(name=docker_code_repo.name)],
                build_parameters={
                    "CONTEXT": f"{docker_code_repo.name}/src/ol_superset",
                    "BUILD_ARG_SUPERSET_TAG": "((.:superset_tag))",
                },
                build_args=[],
            ),
            PutStep(
                put=superset_image.name,
                inputs="all",
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"{docker_code_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )


def create_git_resource(identifier: str, branch: str, paths: list[str]) -> Resource:
    """Create standardized git repo resource"""
    return git_repo(
        Identifier(f"ol-inf-superset-{identifier}"),
        uri="https://github.com/mitodl/ol_infrastructure",
        branch=branch,
        paths=paths,
    )


class PipelineError(Exception):
    """Base exception for pipeline errors."""


class InvalidPipelineTypeError(PipelineError):
    """Exception raised when an invalid pipeline type is provided."""

    def __init__(self, pipeline_type: str) -> None:
        self.pipeline_type = pipeline_type
        super().__init__(f"Invalid pipeline type: {pipeline_type}")


def build_superset_pipeline(pipeline_type: Literal["main", "ci"] = "main") -> Pipeline:
    """Build pipeline with specified configuration

    Args:
        pipeline_type: Type of pipeline to build ("main" or "ci")

    Returns:
        Pipeline configuration

    Raises:
        InvalidPipelineTypeError: If pipeline_type is not "main" or "ci"
    """
    if pipeline_type not in PIPELINE_CONFIG:
        raise InvalidPipelineTypeError(pipeline_type)

    config = PIPELINE_CONFIG[pipeline_type]

    superset_release, docker_code_repo, superset_image = create_base_resources(
        branch=config["branch"],
        environment=config["environment"],
    )

    packer_code_repo = create_git_resource(
        identifier="packer-code",
        branch=config["branch"],
        paths=["src/bilder/components/", "src/bilder/images/superset/"],
    )

    pulumi_code_repo = create_git_resource(
        identifier="pulumi-code",
        branch=config["branch"],
        paths=[
            *PULUMI_WATCHED_PATHS,
            "src/ol_infrastructure/applications/superset/",
            "src/bridge/secrets/superset",
        ],
    )

    docker_build_job = create_build_job(
        name=f"build-superset{'-ci' if pipeline_type == 'ci' else ''}-image",
        superset_release=superset_release,
        docker_code_repo=docker_code_repo,
        superset_image=superset_image,
    )

    packer_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=superset_image.name,
                trigger=True,
                passed=[docker_build_job.name],
            ),
        ],
        image_code=packer_code_repo,
        packer_template_path="src/bilder/images/superset/superset.pkr.hcl",
        env_vars_from_files={"SUPERSET_IMAGE_SHA": f"{superset_image.name}/digest"},
        extra_packer_params={
            "only": [config["packer_build"]],
        },
    )

    pulumi_fragment = pulumi_jobs_chain(
        pulumi_code_repo,
        stack_names=config["stacks"],
        project_name="ol-infrastructure-superset-server",
        project_source_path=PULUMI_CODE_PATH.joinpath("applications/superset/"),
        dependencies=[
            GetStep(
                get=packer_fragment.resources[-1].name,
                trigger=True,
                passed=[packer_fragment.jobs[-1].name],
            )
        ],
    )

    combined_fragment = create_pipeline_fragment(
        docker_build_job,
        packer_fragment,
        pulumi_fragment,
        [
            docker_code_repo,
            packer_code_repo,
            pulumi_code_repo,
            superset_image,
            superset_release,
        ],
    )

    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=combined_fragment.resources,
        jobs=combined_fragment.jobs,
    )


class PipelineOutputConfig(TypedDict):
    path: Path
    name: str


if __name__ == "__main__":
    output_dir = Path.cwd()
    pipelines: dict[Literal["ci", "main"], PipelineOutputConfig] = {
        "ci": {
            "path": output_dir / "ci-definition.json",
            "name": "docker-packer-pulumi-superset-ci",
        },
        "main": {
            "path": output_dir / "definition.json",
            "name": "docker-packer-pulumi-superset",
        },
    }

    for pipeline_type in ("main", "ci"):
        pipeline_config = pipelines[pipeline_type]
        pipeline_def = build_superset_pipeline(pipeline_type=pipeline_type)
        pipeline_config["path"].write_text(pipeline_def.json(indent=2))

    # Output commands to set both pipelines
    fly_commands = [
        f"fly -t pr-inf sp -p {config['name']} -c {config['path']}\n"
        for config in pipelines.values()
    ]
    sys.stdout.writelines(fly_commands)
