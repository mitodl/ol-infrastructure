# ruff: noqa: PLR0913

import sys
from typing import Any, Optional, Union

from pydantic import BaseModel, model_validator

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import pulumi_job, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    ResourceType,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import pulumi_provisioner_resource
from ol_concourse.lib.resources import git_repo, pulumi_provisioner, registry_image
from ol_concourse.pipelines.constants import PULUMI_WATCHED_PATHS


class AppPipelineParams(BaseModel):
    app_name: str
    repo_name: Optional[str] = None
    dockerfile_path: str = "./Dockerfile"
    build_target: Optional[str] = None

    @model_validator(mode="after")
    def set_repo_name(self) -> "AppPipelineParams":
        """Set the repo_name based on the app_name if not provided."""
        if not self.repo_name:
            self.repo_name = self.app_name
        return self


pipeline_params = {
    "mitxonline": AppPipelineParams(app_name="mitxonline", build_target="production"),
    "mit-learn-nextjs": AppPipelineParams(
        app_name="mit-learn-nextjs",
        build_target="build_skip_yarn",
        repo_name="mit-learn",
    ),
}


def _define_git_resources(
    app_name: str,
    repo_name: Union[str, None],
) -> tuple[Resource, Resource, Resource, Resource]:
    """Define the git resources needed for the pipeline."""
    main_repo = git_repo(
        name=Identifier(f"{app_name}-main"),
        uri=f"https://github.com/mitodl/{repo_name}",
        branch="main",
    )

    release_candidate_repo = git_repo(
        name=Identifier(f"{app_name}-release-candidate"),
        uri=f"https://github.com/mitodl/{repo_name}",
        branch="release-candidate",
        fetch_tags=True,
    )

    release_repo = git_repo(
        name=Identifier(f"{app_name}-release"),
        uri=f"http://github.com/mitodl/{repo_name}",
        branch="release",
        fetch_tags=True,
        tag_regex=r"v[0-9]\.[0-9]*\.[0-9]",  # examples v0.24.0, v0.26.3
    )

    ol_infra_repo = git_repo(
        Identifier(f"ol-infra-{app_name}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}",
            *PULUMI_WATCHED_PATHS,
        ],
    )
    return (
        main_repo,
        release_candidate_repo,
        release_repo,
        ol_infra_repo,
    )


def _define_registry_image_resources(app_name: str) -> tuple[Resource, Resource]:
    """Define the registry image resources needed for the pipeline."""
    # CI image resource - tagged 'latest' and git short ref, pushed by main build
    ci_image = registry_image(
        name=Identifier(f"{app_name}-app-ci-image"),
        image_repository=f"mitodl/{app_name}-app",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
        check_every="never",  # Only updated via put step from main build job
    )
    # RC/Production image resource - tagged with version, pushed by RC build
    rc_image = registry_image(
        name=Identifier(f"{app_name}-app-release-image"),
        image_repository=f"mitodl/{app_name}-app",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
        check_every="never",  # Only updated via put step from rc build job
        image_tag=None,
        # While check_every=never, defining tag_regex helps Concourse UI understand
        # resource versions
        tag_regex=r"[0-9]+\.[0-9]+\.[0-9]+",  # examples 0.24.0, 0.26.3
        sort_by_creation=True,
    )
    return ci_image, rc_image


def _define_pulumi_resources(
    app_name: str, ol_infra_repo_name: str
) -> tuple[ResourceType, str]:
    """Define the Pulumi resource type and resource."""
    pulumi_resource_type = pulumi_provisioner_resource()
    pulumi_resource = pulumi_provisioner(
        name=Identifier(f"pulumi-ol-infrastructure-{app_name}-application"),
        project_name=f"ol-infrastructure-{app_name}-application",
        project_path=(
            f"{ol_infra_repo_name}/src/ol_infrastructure/applications/"
            f"{app_name.replace('-', '_')}"
        ),
    )
    return pulumi_resource_type, pulumi_resource


def _build_image_job(
    app_name: str,
    branch_type: str,
    dockerfile_path: str,
    git_repo_resource: Resource,
    registry_image_resource: Resource,
    build_target: Optional[str] = None,
) -> Job:
    """Generate an image build job for a specific branch type (main or rc)."""
    job_name = f"build-{app_name}-image-from-{branch_type}"
    version_var = f"{branch_type}_version"
    version_output_dir = f"{branch_type}_version"
    version_file = f"{version_output_dir}/version"

    plan = [
        GetStep(get=git_repo_resource.name, trigger=True),
    ]

    # Add version extraction steps only for release_candidate
    version_args = {}
    additional_build_params = {}
    if build_target:
        additional_build_params = {
            "TARGET": build_target,
        }
    if branch_type == "release_candidate":
        plan.extend(
            [
                TaskStep(
                    task=Identifier("fetch-rc-version"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type=REGISTRY_IMAGE,
                            source=RegistryImage(repository="alpine"),
                        ),
                        inputs=[Input(name=git_repo_resource.name)],
                        outputs=[Output(name=Identifier(version_output_dir))],
                        run=Command(
                            path="sh",
                            args=[
                                "-c",
                                f"grep 'VERSION = ' {git_repo_resource.name}/main/settings.py | cut -d'\"' -f2 > {version_file}",  # noqa: E501
                            ],
                        ),
                    ),
                ),
                LoadVarStep(
                    load_var=version_var,
                    file=version_file,
                    reveal=True,
                ),
            ]
        )
        version_args = {"BUILD_ARG_RELEASE_VERSION": f"((.:{version_var}))"}

    plan.extend(
        [
            LoadVarStep(
                load_var="git_ref",
                file=f"{git_repo_resource.name}/.git/ref",
                reveal=True,
            ),
            container_build_task(
                inputs=[Input(name=git_repo_resource.name)],
                build_parameters={
                    "CONTEXT": git_repo_resource.name,
                    "DOCKERFILE": f"{git_repo_resource.name}/{dockerfile_path}",
                    "BUILD_ARG_GIT_REF": "((.:git_ref))",
                    **version_args,
                    **additional_build_params,
                },
                build_args=[],
            ),
        ]
    )

    put_params: dict[str, Any] = {
        "image": "image/image.tar",
        "additional_tags": f"./{git_repo_resource.name}/.git/short_ref",
    }
    if branch_type != "main":
        put_params["version"] = f"((.:{version_var}))"
        put_params["bump_aliases"] = True

    plan.append(PutStep(put=registry_image_resource.name, params=put_params))

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def build_app_pipeline(app_name: str) -> Pipeline:
    pipeline_parameters = pipeline_params.get(
        app_name, AppPipelineParams(app_name=app_name)
    )
    # Define Resources
    (
        main_repo,
        release_candidate_repo,
        release_repo,
        ol_infra_repo,
    ) = _define_git_resources(app_name, pipeline_parameters.repo_name)
    app_ci_image, app_rc_image = _define_registry_image_resources(app_name)
    pulumi_resource_type, pulumi_resource = _define_pulumi_resources(
        app_name, ol_infra_repo.name
    )

    # Retrieve any special configurations needed from mapping above,
    # default to no special configuration if app name is not found in mapping

    # Define Build Jobs
    main_image_build_job = _build_image_job(
        app_name=app_name,
        branch_type="main",
        dockerfile_path=pipeline_parameters.dockerfile_path,
        git_repo_resource=main_repo,
        registry_image_resource=app_ci_image,
        build_target=pipeline_parameters.build_target,
    )
    rc_image_build_job = _build_image_job(
        app_name=app_name,
        branch_type="release_candidate",
        dockerfile_path=pipeline_parameters.dockerfile_path,
        git_repo_resource=release_candidate_repo,
        registry_image_resource=app_rc_image,
        build_target=pipeline_parameters.build_target,
    )

    # Define Deployment Jobs

    # CI Deployment
    ci_fragment = pulumi_job(
        pulumi_code=ol_infra_repo,
        stack_name=f"applications.{app_name.replace('-', '_')}.CI",
        project_name=f"ol-infrastructure-{app_name}-application",
        project_source_path=(
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}"
        ),
        dependencies=[
            GetStep(
                get=app_ci_image.name,
                trigger=True,
                passed=[main_image_build_job.name],
                params={"skip_download": True},
            ),
            LoadVarStep(
                load_var="image_tag", file=f"{app_ci_image.name}/tag", reveal=True
            ),
        ],
        additional_env_vars={
            f"{app_name.replace('-', '_').upper()}_DOCKER_TAG": "((.:image_tag))",
        },
    )

    # QA and Production Deployments
    qa_and_production_fragment = pulumi_jobs_chain(
        pulumi_code=ol_infra_repo,
        stack_names=[
            f"applications.{app_name.replace('-', '_')}.QA",
            f"applications.{app_name.replace('-', '_')}.Production",
        ],
        project_name=f"ol-infrastructure-{app_name}-application",
        project_source_path=(
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}"
        ),
        dependencies=[
            GetStep(
                get=app_rc_image.name,
                trigger=True,
                passed=[rc_image_build_job.name],
                params={"skip_download": True},
            ),
            LoadVarStep(
                load_var="image_tag", file=f"{app_rc_image.name}/tag", reveal=True
            ),
        ],
        additional_env_vars={
            f"{app_name.replace('-', '_').upper()}_DOCKER_TAG": "((.:image_tag))",
        },
        enable_github_issue_resource=False,
    )

    # Trigger a production deploy when the release branch is updated
    qa_and_production_fragment.jobs[-1].plan.insert(
        0, GetStep(get=release_repo.name, trigger=True)
    )

    # Group into Fragments

    main_branch_container_fragement = PipelineFragment(
        resources=[main_repo, app_ci_image], jobs=[main_image_build_job]
    )

    release_candidate_container_fragment = PipelineFragment(
        resources=[release_candidate_repo, app_rc_image],
        jobs=[rc_image_build_job],
    )

    # Consolidate resources for deployment fragments
    deployment_resources = [
        ol_infra_repo,
        pulumi_resource,
        release_repo,
        app_ci_image,  # Needed for CI deployment trigger
        app_rc_image,  # Needed for QA/Prod deployment trigger
    ]

    ci_deployment_fragment = PipelineFragment(
        resource_types=[pulumi_resource_type, *ci_fragment.resource_types],
        resources=[*deployment_resources, *ci_fragment.resources],
        jobs=ci_fragment.jobs,
    )

    # Update qa_and_production_fragment resources similarly if needed,
    # though pulumi_jobs_chain handles its own resource management internally.
    # Ensure app_image is available to qa_and_production_fragment jobs.
    # The dependency GetStep already references app_image.name.

    # Combine all fragments

    combined_fragment = PipelineFragment.combine_fragments(
        ci_deployment_fragment,
        qa_and_production_fragment,
        main_branch_container_fragement,
        release_candidate_container_fragment,
    )
    return combined_fragment.to_pipeline()


if __name__ == "__main__":
    import sys

    app_name = sys.argv[1] if len(sys.argv) > 1 else None
    if not app_name:
        msg = "Please provide an app name as a command line argument."
        raise ValueError(msg)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(
            build_app_pipeline(app_name=app_name).model_dump_json(indent=2)
        )
    sys.stdout.write(build_app_pipeline(app_name=app_name).model_dump_json(indent=2))
    # Note: The pipeline name generated below might need adjustment
    # if the app_name changes the resulting pipeline identifier.
    pipeline_name = f"docker-pulumi-{app_name}"
    sys.stdout.writelines(
        (
            "\n",
            f"fly -t pr-inf sp -p {pipeline_name} -c definition.json",
        )
    )
