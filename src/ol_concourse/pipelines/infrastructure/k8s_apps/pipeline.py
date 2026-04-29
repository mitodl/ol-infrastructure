# ruff: noqa: PLR0913, E501
"""Generate Concourse pipeline definitions for building and deploying dockerized applications to Kubernetes via Pulumi."""

import sys
from typing import Any

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Pipeline,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    ResourceType,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import (
    github_deployments_resource,
    pulumi_provisioner_resource,
    release_resource_type,
)
from ol_concourse.lib.resources import (
    git_repo,
    github_deployment,
    github_issues,
    pulumi_provisioner,
    registry_image,
    release_resource,
)
from ol_concourse.lib.tasks import bump_version_task
from pydantic import BaseModel, model_validator

from ol_concourse.pipelines.constants import (
    ECR_REGION,
    PULUMI_WATCHED_PATHS,
    dockerhub_ecr_image_uri,
)
from ol_concourse.pipelines.jobs import pulumi_job, pulumi_jobs_chain


class AppPipelineParams(BaseModel):
    """Parameters for the application pipeline.

    This class defines the parameters needed to configure the pipeline for
    different applications, including the app name, build target, Dockerfile path,
    Fastly service prefix, cache purging options, and repository name.

    Attributes:
        app_name (str): The name of the application.
        build_target (Optional[str]): The specific target stage to build within the Dockerfile.
        dockerfile_path (str): The path to the Dockerfile within the repository. Defaults to "./Dockerfile".
        fastly_domains (Optional[dict[str, str]]): Per-environment hostnames served by the Fastly
            service for this app.  When set together with ``purge_fastly_cache=True``, the
            pipeline registers a Fastly resource for each environment and resolves the service ID
            automatically from the domain name at pipeline runtime — no opaque service IDs need to
            be stored in Vault.  Keys must be ``"ci"``, ``"qa"``, and ``"production"``.
        purge_fastly_cache (bool): Whether to include steps to purge the Fastly cache after deployment. Defaults to False.
        fastly_purge_scope (str): Controls which Fastly purge endpoint is called when purge_fastly_cache is True.
            Use "purge_all" (the default) to purge the entire service cache via POST /purge_all.
            Pass a surrogate-key string (e.g. "html-pages") to purge only objects tagged with
            that key via POST /purge/{surrogate_key}.  Use a scoped key when some cached assets
            (e.g. content-addressed static files) must survive a deployment.
        repo_name (Optional[str]): The name of the git repository. Defaults to app_name if not provided.
        github_repo (Optional[str]): The GitHub repository in ``owner/repo`` form used for release resources
            and GitHub Deployments. Defaults to ``mitodl/{repo_name}``.
    """

    app_name: str
    build_target: str | None = None
    dockerfile_path: str = "./Dockerfile"
    fastly_domains: dict[str, str] | None = None
    purge_fastly_cache: bool = False
    fastly_purge_scope: str = "purge_all"
    repo_name: str | None = None
    repo_main_branch: str = "main"
    ol_infra_branch: str = "main"
    settings_dir: str = "main"
    github_repo: str | None = None

    @model_validator(mode="after")
    def set_repo_name(self) -> "AppPipelineParams":
        """Set the repo_name and github_repo based on app_name if not provided."""
        if not self.repo_name:
            self.repo_name = self.app_name
        if not self.github_repo:
            self.github_repo = f"mitodl/{self.repo_name}"
        return self

    @model_validator(mode="after")
    def validate_fastly_config(self) -> "AppPipelineParams":
        """Enforce that fastly_domains is fully specified when cache purging is enabled.

        Raises:
            ValueError: if ``purge_fastly_cache`` is True but ``fastly_domains`` is
                not set, or if any of the required keys (``ci``, ``qa``,
                ``production``) are missing from ``fastly_domains``.
        """
        if not self.purge_fastly_cache:
            return self
        if self.fastly_domains is None:
            msg = (
                f"{self.app_name}: purge_fastly_cache=True requires fastly_domains "
                "to be set with keys 'ci', 'qa', and 'production'."
            )
            raise ValueError(msg)
        required_envs = {"ci", "qa", "production"}
        missing = required_envs - self.fastly_domains.keys()
        if missing:
            msg = (
                f"{self.app_name}: fastly_domains is missing required environment "
                f"keys: {sorted(missing)}"
            )
            raise ValueError(msg)
        return self


pipeline_params = {
    "micromasters": AppPipelineParams(
        app_name="micromasters",
        build_target="production",
        repo_main_branch="master",
        settings_dir="micromasters",
    ),
    "mitxonline": AppPipelineParams(app_name="mitxonline", build_target="production"),
    "mit-learn-nextjs": AppPipelineParams(
        app_name="mit-learn-nextjs",
        # No build_target: use the default `runner` stage, which bakes `yarn build`
        # into the Docker image via `output: "standalone"` in next.config.js.
        # NEXT_PUBLIC_* values are injected at runtime from Kubernetes env vars.
        repo_name="mit-learn",
        dockerfile_path="frontends/main/Dockerfile.web",
        purge_fastly_cache=True,
        fastly_domains={
            "ci": "ci.learn.mit.edu",
            "qa": "rc.learn.mit.edu",
            "production": "learn.mit.edu",
        },
        fastly_purge_scope="html-pages",
    ),
    "xpro": AppPipelineParams(
        app_name="xpro",
        repo_name="mitxpro",
        repo_main_branch="master",
        build_target="production",
        settings_dir="mitxpro",
    ),
    "ocw-studio": AppPipelineParams(
        app_name="ocw-studio",
        repo_main_branch="master",
        build_target="production",
    ),
    "odl-video-service": AppPipelineParams(
        app_name="odl-video-service",
        repo_main_branch="master",
        build_target="production",
        settings_dir="odl_video",
    ),
}


def _define_git_resources(
    app_name: str,
    repo_name: str | None,
    repo_main_branch: str,
    ol_infra_branch: str,
) -> tuple[Resource, Resource]:
    """Define the git resources needed for the pipeline."""
    main_repo = git_repo(
        name=Identifier(f"{app_name}-{repo_main_branch}"),
        uri=f"https://github.com/mitodl/{repo_name}",
        branch=repo_main_branch,
    )

    ol_infra_repo = git_repo(
        Identifier(f"ol-infra-{app_name}"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch=ol_infra_branch,
        paths=[
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}",
            *PULUMI_WATCHED_PATHS,
        ],
    )
    return main_repo, ol_infra_repo


def _define_release_resources(
    app_name: str,
    github_repo: str,
    repo_main_branch: str = "main",
) -> tuple[Resource, Resource, Resource, Resource, Resource]:
    """Define the release-flow resources: release resource, gates, issues, and GitHub Deployments."""
    release_res = release_resource(
        name=Identifier(f"{app_name}-release"),
        uri=f"https://github.com/{github_repo}",
        branch=repo_main_branch,
        access_token="((github.access_token))",  # noqa: S106
        repository=github_repo,
    )
    # Closed release issues gate production deployments.
    release_gate = github_issues(
        name=Identifier(f"{app_name}-release-gate"),
        repository=github_repo,
        issue_prefix=f"Release {app_name}",
        issue_title_template=f"Release {app_name}",
        issue_state="closed",
        skip_if_labeled=["abandoned"],
        gh_host=None,
    )
    # Open release issues are created after each QA deployment.
    release_issue = github_issues(
        name=Identifier(f"{app_name}-release-issue"),
        repository=github_repo,
        issue_prefix=f"Release {app_name}",
        issue_title_template=f"Release {app_name}",
        issue_state="open",
        labels=["release"],
        gh_host=None,
    )
    deployment_rc = github_deployment(
        name=Identifier(f"{app_name}-deployment-rc"),
        repository=github_repo,
        environment="RC",
    )
    deployment_prod = github_deployment(
        name=Identifier(f"{app_name}-deployment-production"),
        repository=github_repo,
        environment="Production",
    )
    return release_res, release_gate, release_issue, deployment_rc, deployment_prod


def _define_registry_image_resources(
    app_name: str,
) -> tuple[Resource, Resource, Resource, Resource]:
    """Define the registry image resources needed for the pipeline."""
    dockerhub_kwargs = {
        "username": "((dockerhub.username))",
        "password": "((dockerhub.password))",
    }
    ecr_kwargs = {"ecr_region": "us-east-1"}
    # CI image resource - tagged 'latest' and git short ref, pushed by main build
    docker_ci_image = registry_image(
        name=Identifier(f"{app_name}-app-ci-image"),
        image_repository=f"mitodl/{app_name}-app",
        check_every="never",  # Only updated via put step from main build job
        **dockerhub_kwargs,
    )
    # RC/Production image resource - tagged with version, pushed by RC build
    docker_rc_image = registry_image(
        name=Identifier(f"{app_name}-app-release-image"),
        image_repository=f"mitodl/{app_name}-app",
        check_every="never",  # Only updated via put step from rc build job
        image_tag=None,
        # While check_every=never, defining tag_regex helps Concourse UI understand
        # resource versions
        tag_regex=r"[0-9]+\.[0-9]+\.[0-9]+",  # examples 0.24.0, 0.26.3
        sort_by_creation=True,
        **dockerhub_kwargs,
    )

    ecr_ci_image = registry_image(
        name=Identifier(f"{app_name}-app-ci-ecr-image"),
        image_repository=f"mitodl/{app_name}-app",
        check_every="never",  # Only updated via put step from main build job
        **ecr_kwargs,
    )
    ecr_rc_image = registry_image(
        name=Identifier(f"{app_name}-app-release-ecr-image"),
        image_repository=f"mitodl/{app_name}-app",
        check_every="never",  # Only updated via put step from rc build job
        image_tag=None,
        # While check_every=never, defining tag_regex helps Concourse UI understand
        # resource versions
        tag_regex=r"[0-9]+\.[0-9]+\.[0-9]+",  # examples 0.24.0, 0.26.3
        sort_by_creation=True,
        **ecr_kwargs,
    )
    return docker_ci_image, docker_rc_image, ecr_ci_image, ecr_rc_image


def _define_fastly_resources(
    app_name: str,
    fastly_domains: dict[str, str],
) -> tuple[ResourceType, Resource, Resource, Resource]:
    """Define the Fastly resource type and per-environment cache-purge resources.

    :param app_name: The application name; used as a prefix for resource identifiers.
    :param fastly_domains: Mapping of environment name to the hostname served by the
        Fastly service for that environment (e.g.
        ``{"ci": "next.ci.learn.mit.edu", "qa": "next.rc.learn.mit.edu",
        "production": "next.learn.mit.edu"}``).  The Fastly resource resolves the
        service ID automatically from the domain at pipeline runtime — no opaque
        service IDs need to be stored in Vault.
    :returns: A 4-tuple of
        ``(resource_type, ci_resource, qa_resource, production_resource)``.
    """
    fastly_resource_type = ResourceType(
        name=Identifier("fastly"),
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-fastly-resource"),
    )

    def _env_resource(env: str) -> Resource:
        return Resource(
            name=Identifier(f"{app_name}-fastly-{env}"),
            type="fastly",
            check_every="never",
            source={
                "api_token": "((fastly.fastly_api_token))",
                "domain": fastly_domains[env],
            },
        )

    return (
        fastly_resource_type,
        _env_resource("ci"),
        _env_resource("qa"),
        _env_resource("production"),
    )


def _fastly_purge_params(purge_scope: str) -> dict[str, str]:
    """Return ``put`` params for a Fastly purge step.

    :param purge_scope: Either ``"purge_all"`` to purge the entire service cache
        (``mode: purge_all``), or a surrogate-key string (e.g. ``"html-pages"``)
        to purge only objects tagged with that key (``mode: surrogate_key``).
    :returns: A ``params`` dict suitable for a Concourse :class:`PutStep`.
    """
    if purge_scope == "purge_all":
        return {"mode": "purge_all"}
    return {"mode": "surrogate_key", "surrogate_key": purge_scope}


def _define_pulumi_resources(
    app_name: str, ol_infra_repo_name: str
) -> tuple[ResourceType, str]:
    """Define the Pulumi resource type and resource."""
    pulumi_resource_type = pulumi_provisioner_resource()
    pulumi_resource = pulumi_provisioner(
        name=Identifier(f"pulumi-ol-application-{app_name}"),
        project_name=f"ol-application-{app_name}",
        project_path=(
            f"{ol_infra_repo_name}/src/ol_infrastructure/applications/"
            f"{app_name.replace('-', '_')}"
        ),
    )
    return pulumi_resource_type, pulumi_resource


def _build_image_job(
    app_name: str,
    dockerfile_path: str,
    git_repo_resource: Resource,
    dockerhub_registry_image_resource: Resource,
    ecr_registry_image_resource: Resource,
    build_target: str | None = None,
) -> Job:
    """Generate an image build job triggered by the configured git resource."""
    job_name = f"build-{app_name}-image-from-{git_repo_resource.source['branch']}"

    additional_build_params = {}
    if build_target:
        additional_build_params = {"TARGET": build_target}

    plan = [
        GetStep(get=git_repo_resource.name, trigger=True),
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
                **additional_build_params,
            },
            build_args=[],
        ),
        TaskStep(
            task=Identifier("ensure-ecr-repository"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={
                        "repository": dockerhub_ecr_image_uri("amazon/aws-cli"),
                        "tag": "latest",
                        "aws_region": ECR_REGION,
                    },
                ),
                params={
                    "REPO_NAME": ecr_registry_image_resource.source["repository"],
                    "AWS_PAGER": "cat",
                },
                run=Command(
                    path="sh",
                    args=[
                        "-exc",
                        "aws ecr describe-repositories --repository-names ${REPO_NAME} || aws ecr create-repository --repository-name ${REPO_NAME}",
                    ],
                ),
            ),
        ),
        PutStep(
            put=dockerhub_registry_image_resource.name,
            params={
                "image": "image/image.tar",
                "additional_tags": f"./{git_repo_resource.name}/.git/short_ref",
            },
        ),
        PutStep(
            put=ecr_registry_image_resource.name,
            params={
                "image": "image/image.tar",
                "additional_tags": f"./{git_repo_resource.name}/.git/short_ref",
            },
        ),
    ]

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def _build_release_image_job(
    app_name: str,
    dockerfile_path: str,
    main_repo: Resource,
    release_res: Resource,
    dockerhub_registry_image_resource: Resource,
    ecr_registry_image_resource: Resource,
    build_target: str | None = None,
) -> Job:
    """Generate an image build job triggered by the release resource.

    This job:
    1. Gets the release resource (trigger) and main repo source.
    2. Bumps the version in the app source using bumpver.
    3. Creates the release commit, branch, and tag via the release resource.
    4. Builds and pushes a versioned Docker image to DockerHub and ECR.
    """
    job_name = f"build-{app_name}-release-image"

    additional_build_params = {}
    if build_target:
        additional_build_params = {"TARGET": build_target}

    put_params: dict[str, Any] = {
        "image": "image/image.tar",
        "additional_tags": f"./{release_res.name}/.git/short_ref",
        "version": "((.:release_version))",
        "bump_aliases": True,
    }

    plan = [
        GetStep(get=release_res.name, trigger=True),
        GetStep(get=main_repo.name, trigger=False),
        LoadVarStep(
            load_var="release_version",
            file=f"{release_res.name}/version",
            reveal=True,
        ),
        bump_version_task(
            version_file=f"{release_res.name}/version",
            repository=str(main_repo.name),
        ),
        PutStep(
            put=release_res.name,
            params={
                "action": "create",
                "repo_dir": str(main_repo.name),
                "version_file": f"{release_res.name}/version",
            },
        ),
        # Load git_ref from the release resource AFTER the release commit is created,
        # so the image is stamped with the correct post-release commit SHA.
        LoadVarStep(
            load_var="git_ref",
            file=f"{release_res.name}/.git/ref",
            reveal=True,
        ),
        container_build_task(
            inputs=[Input(name=release_res.name)],
            build_parameters={
                "CONTEXT": release_res.name,
                "DOCKERFILE": f"{release_res.name}/{dockerfile_path}",
                "BUILD_ARG_GIT_REF": "((.:git_ref))",
                "BUILD_ARG_RELEASE_VERSION": "((.:release_version))",
                **additional_build_params,
            },
            build_args=[],
        ),
        TaskStep(
            task=Identifier("ensure-ecr-repository"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={
                        "repository": dockerhub_ecr_image_uri("amazon/aws-cli"),
                        "tag": "latest",
                        "aws_region": ECR_REGION,
                    },
                ),
                params={
                    "REPO_NAME": ecr_registry_image_resource.source["repository"],
                    "AWS_PAGER": "cat",
                },
                run=Command(
                    path="sh",
                    args=[
                        "-exc",
                        "aws ecr describe-repositories --repository-names ${REPO_NAME} || aws ecr create-repository --repository-name ${REPO_NAME}",
                    ],
                ),
            ),
        ),
        PutStep(put=dockerhub_registry_image_resource.name, params=put_params),
        PutStep(put=ecr_registry_image_resource.name, params=put_params),
    ]

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def build_app_pipeline(app_name: str) -> Pipeline:
    """Generate the full Concourse pipeline for a given application.

    This function orchestrates all the resources and jobs required to build, test,
    and deploy a dockerized application to Kubernetes.
    """
    pipeline_parameters = pipeline_params.get(
        app_name, AppPipelineParams(app_name=app_name)
    )
    # Define Resources
    main_repo, ol_infra_repo = _define_git_resources(
        app_name=app_name,
        repo_name=pipeline_parameters.repo_name,
        repo_main_branch=pipeline_parameters.repo_main_branch,
        ol_infra_branch=pipeline_parameters.ol_infra_branch,
    )
    (
        docker_ci_image,
        docker_rc_image,
        app_ci_image,
        app_rc_image,
    ) = _define_registry_image_resources(app_name)
    pulumi_resource_type, pulumi_resource = _define_pulumi_resources(
        app_name, ol_infra_repo.name
    )

    # Optionally define Fastly resources for post-deployment cache purging
    fastly_rtype: ResourceType | None = None
    fastly_ci: Resource | None = None
    fastly_qa: Resource | None = None
    fastly_prod: Resource | None = None

    if pipeline_parameters.purge_fastly_cache and pipeline_parameters.fastly_domains:
        fastly_rtype, fastly_ci, fastly_qa, fastly_prod = _define_fastly_resources(
            app_name=app_name,
            fastly_domains=pipeline_parameters.fastly_domains,
        )

    (
        release_res,
        release_gate,
        release_issue,
        deployment_rc,
        deployment_prod,
    ) = _define_release_resources(
        app_name=app_name,
        github_repo=pipeline_parameters.github_repo,
        repo_main_branch=pipeline_parameters.repo_main_branch,
    )

    # Define Build Jobs
    main_image_build_job = _build_image_job(
        app_name=app_name,
        dockerfile_path=pipeline_parameters.dockerfile_path,
        git_repo_resource=main_repo,
        dockerhub_registry_image_resource=docker_ci_image,
        ecr_registry_image_resource=app_ci_image,
        build_target=pipeline_parameters.build_target,
    )
    release_image_build_job = _build_release_image_job(
        app_name=app_name,
        dockerfile_path=pipeline_parameters.dockerfile_path,
        main_repo=main_repo,
        release_res=release_res,
        dockerhub_registry_image_resource=docker_rc_image,
        ecr_registry_image_resource=app_rc_image,
        build_target=pipeline_parameters.build_target,
    )

    # Define Deployment Jobs

    # Build CI post-steps list with the correct union type so Pyright is satisfied
    ci_post_steps: list[GetStep | PutStep | TaskStep] = []
    if fastly_ci is not None:
        ci_post_steps.append(
            PutStep(
                put=fastly_ci.name,
                params=_fastly_purge_params(pipeline_parameters.fastly_purge_scope),
                no_get=True,
            )
        )

    # CI Deployment
    ci_fragment = pulumi_job(
        pulumi_code=ol_infra_repo,
        stack_name="CI",
        refresh_stack=True,
        project_name=f"ol-application-{app_name}",
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
                load_var="image_digest",
                file=f"{app_ci_image.name}/digest",
                reveal=True,
            ),
        ],
        additional_post_steps=ci_post_steps,
        additional_env_vars={
            f"{app_name.replace('-', '_').upper()}_DOCKER_SHA": "((.:image_digest))",
        },
        slack_url_path="eks.slack_url",
    )

    # Build per-stack additional_post_steps and custom_dependencies for QA+Production
    qa_post_steps: list[GetStep | PutStep | TaskStep] = [
        # Open a GitHub Release Issue with the checklist from the release resource.
        PutStep(
            put=release_issue.name,
            params={
                "body_file": f"{release_res.name}/checklist.md",
                "labels": ["release"],
            },
        ),
        # Mark the RC GitHub Deployment as successful.
        PutStep(
            put=deployment_rc.name,
            params={
                "action": "finish",
                "deployment_file": f"{deployment_rc.name}/deployment.json",
                "state": "success",
            },
        ),
    ]
    prod_post_steps: list[GetStep | PutStep | TaskStep] = [
        # Mark the Production GitHub Deployment as successful.
        PutStep(
            put=deployment_prod.name,
            params={
                "action": "finish",
                "deployment_file": f"{deployment_prod.name}/deployment.json",
                "state": "success",
            },
        ),
    ]
    if fastly_qa is not None and fastly_prod is not None:
        purge_params = _fastly_purge_params(pipeline_parameters.fastly_purge_scope)
        qa_purge_steps: list[GetStep | PutStep | TaskStep] = []
        qa_purge_steps.append(
            PutStep(put=fastly_qa.name, params=purge_params, no_get=True)
        )
        prod_purge_steps: list[GetStep | PutStep | TaskStep] = []
        prod_purge_steps.append(
            PutStep(put=fastly_prod.name, params=purge_params, no_get=True)
        )
        additional_post_steps = {0: qa_purge_steps, 1: prod_purge_steps}

    # QA and Production Deployments
    qa_and_production_fragment = pulumi_jobs_chain(
        refresh_stack=True,
        pulumi_code=ol_infra_repo,
        stack_names=["QA", "Production"],
        project_name=f"ol-application-{app_name}",
        project_source_path=(
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}"
        ),
        additional_post_steps=additional_post_steps,
        dependencies=[
            GetStep(
                get=app_rc_image.name,
                trigger=True,
                passed=[release_image_build_job.name],
                params={"skip_download": True},
            ),
            LoadVarStep(
                load_var="image_tag", file=f"{app_rc_image.name}/tag", reveal=True
            ),
        ],
        # QA: get checklist.md from release resource; start RC GitHub Deployment.
        # Production: wait for release gate (closed release issue); start prod deployment.
        custom_dependencies={
            0: [
                GetStep(
                    get=release_res.name,
                    trigger=False,
                    passed=[release_image_build_job.name],
                ),
                PutStep(
                    put=deployment_rc.name,
                    params={"action": "start", "ref": "((.:image_tag))"},
                ),
            ],
            1: [
                GetStep(get=release_gate.name, trigger=True),
                PutStep(
                    put=deployment_prod.name,
                    params={"action": "start", "ref": "((.:image_tag))"},
                ),
            ],
        },
        additional_env_vars={
            f"{app_name.replace('-', '_').upper()}_DOCKER_TAG": "((.:image_tag))",
        },
        enable_github_issue_resource=False,
        slack_url_path="eks.slack_url",
    )

    # Group into Fragments

    main_branch_container_fragement = PipelineFragment(
        resources=[main_repo, app_ci_image, docker_ci_image],
        jobs=[main_image_build_job],
    )

    release_container_fragment = PipelineFragment(
        resource_types=[release_resource_type(), github_deployments_resource()],
        resources=[
            release_res,
            release_gate,
            release_issue,
            deployment_rc,
            deployment_prod,
            app_rc_image,
            docker_rc_image,
        ],
        jobs=[release_image_build_job],
    )

    # Consolidate resources for deployment fragments
    deployment_resources = [
        ol_infra_repo,
        pulumi_resource,
        app_ci_image,  # Needed for CI deployment trigger
        app_rc_image,  # Needed for QA/Prod deployment trigger
        docker_ci_image,
        docker_rc_image,
        *(
            [fastly_ci, fastly_qa, fastly_prod]
            if fastly_ci is not None
            and fastly_qa is not None
            and fastly_prod is not None
            else []
        ),
    ]

    ci_deployment_fragment = PipelineFragment(
        resource_types=[
            pulumi_resource_type,
            *ci_fragment.resource_types,
            *([fastly_rtype] if fastly_rtype else []),
        ],
        resources=[*deployment_resources, *ci_fragment.resources],
        jobs=ci_fragment.jobs,
    )

    # Combine all fragments
    combined_fragment = PipelineFragment.combine_fragments(
        ci_deployment_fragment,
        qa_and_production_fragment,
        main_branch_container_fragement,
        release_container_fragment,
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
