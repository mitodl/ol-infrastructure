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
    Output,
    Pipeline,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    ResourceType,
    TaskConfig,
    TaskStep,
    TryStep,
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

from bridge.settings.apps import github_repo as app_github_repo
from bridge.settings.apps import repo_main_branch as app_repo_main_branch
from ol_concourse.pipelines.constants import (
    ECR_REGION,
    PULUMI_WATCHED_PATHS,
    dockerhub_ecr_image_uri,
)
from ol_concourse.pipelines.jobs import pulumi_job, pulumi_jobs_chain


class SentrySourcemapsConfig(BaseModel):
    """Config for uploading an app's source maps to Sentry after the image build.

    Attributes:
        org: Sentry organization slug (e.g. ``"mit-office-of-digital-learning"``).
        project: Sentry project slug (e.g. ``"open-next"``).
        auth_token_vault_key: Concourse credential reference for the Sentry auth
            token (e.g. ``"((sentry.mitlearn_auth_token))"``). The token needs the
            ``org:read`` and ``project:releases`` scopes.
        rootfs_asset_path: Path, relative to the unpacked image rootfs, to the
            directory holding the built JS and ``.map`` files (with debug IDs
            already injected at build time). e.g. ``"app/frontends/main/.next"``
            for the Next.js ``output: "standalone"`` runner image.
    """

    org: str
    project: str
    auth_token_vault_key: str
    rootfs_asset_path: str


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
        version_file (Optional[str]): Repo-relative path to a standalone VERSION file (e.g.
            "VERSION"); if set, overrides the Django settings grep. Only used by the legacy
            (release-candidate/release branch) workflow.
        enable_ci_deploy (bool): Whether to deploy the main-branch image to a CI Pulumi
            stack. Defaults to True. Set False for apps with no CI stack (e.g. a
            dependency is unavailable on the CI cluster) -- the main-branch image is
            still built and published, just not deployed. Only used by the legacy workflow.
        github_repo (Optional[str]): The GitHub repository in ``owner/repo`` form used for release
            resources and GitHub Deployments. Defaults to ``mitodl/{repo_name}``. Only used by the
            release-resource workflow.
        use_release_resource_workflow (bool): Opt an app into the modernized GitHub
            Release/Deployment-based pipeline shape instead of the legacy release-candidate/
            release git-branch pattern. Defaults to False so existing apps are unaffected;
            flip per-app once each has been validated on the new shape.
        sentry_sourcemaps (Optional[SentrySourcemapsConfig]): When set, the app's
            image build unpacks its rootfs and a decoupled task uploads the built
            source maps to Sentry with an auth token -- the token never enters the
            image build. See :class:`SentrySourcemapsConfig`. Left unset, no source
            maps are uploaded.
    """

    app_name: str
    build_target: str | None = None
    dockerfile_path: str = "./Dockerfile"
    fastly_domains: dict[str, str] | None = None
    purge_fastly_cache: bool = False
    fastly_purge_scope: str = "purge_all"
    repo_name: str | None = None
    repo_main_branch: str = "main"
    repo_rc_branch: str = "release-candidate"
    repo_release_branch: str = "release"
    ol_infra_branch: str = "main"
    settings_dir: str = "main"
    version_file: str | None = None
    enable_ci_deploy: bool = True
    github_repo: str | None = None
    use_release_resource_workflow: bool = False
    sentry_sourcemaps: SentrySourcemapsConfig | None = None

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
        repo_main_branch=app_repo_main_branch("micromasters"),
        settings_dir="micromasters",
        version_file="VERSION",
    ),
    "mitxonline": AppPipelineParams(app_name="mitxonline", build_target="production"),
    "mit-learn-nextjs": AppPipelineParams(
        app_name="mit-learn-nextjs",
        # No build_target: use the default `runner` stage, which bakes `yarn build`
        # into the Docker image via `output: "standalone"` in next.config.js.
        # NEXT_PUBLIC_* values are injected at runtime from Kubernetes env vars.
        repo_name=app_github_repo("mit-learn-nextjs").split("/", 1)[1],
        dockerfile_path="frontends/main/Dockerfile.web",
        purge_fastly_cache=True,
        fastly_domains={
            "ci": "ci.learn.mit.edu",
            "qa": "rc.learn.mit.edu",
            "production": "learn.mit.edu",
        },
        fastly_purge_scope="html-pages",
        sentry_sourcemaps=SentrySourcemapsConfig(
            org="mit-office-of-digital-learning",
            project="open-next",
            auth_token_vault_key="((sentry.mitlearn_auth_token))",  # noqa: S106  # pragma: allowlist secret
            rootfs_asset_path="app/frontends/main/.next",
        ),
    ),
    "xpro": AppPipelineParams(
        app_name="xpro",
        repo_name=app_github_repo("xpro").split("/", 1)[1],
        repo_main_branch=app_repo_main_branch("xpro"),
        build_target="production",
        settings_dir="mitxpro",
    ),
    "ocw-studio": AppPipelineParams(
        app_name="ocw-studio",
        repo_main_branch=app_repo_main_branch("ocw-studio"),
        build_target="production",
    ),
    "odl-video-service": AppPipelineParams(
        app_name="odl-video-service",
        repo_main_branch=app_repo_main_branch("odl-video-service"),
        build_target="production",
        settings_dir="odl_video",
    ),
    "ol-analytics-api": AppPipelineParams(
        app_name="ol-analytics-api",
        use_release_resource_workflow=True,
    ),
}


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


def _ensure_ecr_repository_step(ecr_registry_image_resource: Resource) -> TaskStep:
    """Return the shared 'create the ECR repo if it does not exist yet' step."""
    return TaskStep(
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
    )


# ============================================================================
# Legacy workflow: release-candidate/release git-branch pattern.
# Used by every app except those with use_release_resource_workflow=True.
# ============================================================================


def _define_git_resources_legacy(
    app_name: str,
    repo_name: str | None,
    repo_main_branch: str,
    repo_rc_branch: str,
    repo_release_branch: str,
    ol_infra_branch: str,
) -> tuple[Resource, Resource, Resource, Resource]:
    """Define the git resources needed for the legacy pipeline."""
    main_repo = git_repo(
        name=Identifier(f"{app_name}-{repo_main_branch}"),
        uri=f"https://github.com/mitodl/{repo_name}",
        branch=repo_main_branch,
    )

    release_candidate_repo = git_repo(
        name=Identifier(f"{app_name}-{repo_rc_branch}"),
        uri=f"https://github.com/mitodl/{repo_name}",
        branch=repo_rc_branch,
        fetch_tags=True,
    )

    release_repo = git_repo(
        name=Identifier(f"{app_name}-{repo_release_branch}"),
        uri=f"http://github.com/mitodl/{repo_name}",
        branch=repo_release_branch,
        fetch_tags=True,
        tag_regex=r"v[0-9]+\.[0-9]+\.[0-9]+",  # examples v0.24.0, v0.26.3
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
    return (
        main_repo,
        release_candidate_repo,
        release_repo,
        ol_infra_repo,
    )


def _define_registry_image_resources_legacy(
    app_name: str,
) -> tuple[Resource, Resource, Resource, Resource]:
    """Define the registry image resources needed for the legacy pipeline."""
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


def _sentry_js_sourcemaps_upload_step(
    sentry_config: SentrySourcemapsConfig,
    release_var: str | None = None,
) -> TryStep:
    """Upload injected source maps to Sentry from the built image's rootfs.

    Runs after the image build. Sentry ties errors to source maps by Debug ID (not
    release name); this step expects those already injected in the build output.

    Wrapped in a ``try`` so a failed upload never blocks the release.
    """
    upload_args = [
        "sourcemaps",
        "upload",
        "--org",
        sentry_config.org,
        "--project",
        sentry_config.project,
    ]
    if release_var:
        upload_args.extend(["--release", release_var])
    upload_args.append(f"image/rootfs/{sentry_config.rootfs_asset_path}")

    upload_task = TaskStep(
        task=Identifier("upload-sentry-sourcemaps"),
        attempts=3,
        # sentry-cli auto-reads the SENTRY_AUTH_TOKEN param; Concourse redacts it.
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=AnonymousResource(
                type=REGISTRY_IMAGE,
                source={
                    "repository": dockerhub_ecr_image_uri("getsentry/sentry-cli"),
                    "tag": "3.6.0",
                    "aws_region": ECR_REGION,
                },
                # Pin the exact image by digest; the tag above is for humans.
                version={
                    "digest": "sha256:dd7ad57b7d1609d5dc76705bb9a2b2a7009ea0a1e74089202df8c20cfd8389c4"  # pragma: allowlist secret
                },
            ),
            inputs=[Input(name=Identifier("image"))],
            params={"SENTRY_AUTH_TOKEN": sentry_config.auth_token_vault_key},
            run=Command(path="sentry-cli", args=upload_args),
        ),
    )
    return TryStep(try_=upload_task)


def _build_image_job_legacy(
    app_name: str,
    branch_type: str,
    dockerfile_path: str,
    git_repo_resource: Resource,
    dockerhub_registry_image_resource: Resource,
    ecr_registry_image_resource: Resource,
    build_target: str | None = None,
    django_settings_dir: str = "main",
    repo_version_file: str | None = None,
    sentry_sourcemaps: SentrySourcemapsConfig | None = None,
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
                            source={
                                "repository": dockerhub_ecr_image_uri("alpine"),
                                "tag": "latest",
                                "aws_region": ECR_REGION,
                            },
                        ),
                        inputs=[Input(name=git_repo_resource.name)],
                        outputs=[Output(name=Identifier(version_output_dir))],
                        run=Command(
                            path="sh",
                            args=[
                                "-c",
                                rf"""cat {git_repo_resource.name}/{repo_version_file} > {version_file}"""
                                if repo_version_file
                                else rf"""grep -E -o '^VERSION = "[0-9]+\.[0-9]+\.[0-9]+"$' {git_repo_resource.name}/{django_settings_dir}/settings.py | cut -d\" -f2 > {version_file}""",
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

    # Skip the main build: its image only serves CI. QA and Production both run the
    # RC image, so the RC upload already covers production.
    sourcemap_build_params: dict[str, str] = {}
    sourcemap_upload_step: TryStep | None = None
    if sentry_sourcemaps is not None and branch_type != "main":
        # UNPACK_ROOTFS lets the upload step read the maps out of the built image.
        sourcemap_build_params = {"UNPACK_ROOTFS": "true"}
        # Tag the artifacts with this build's version (== runtime NEXT_PUBLIC_VERSION).
        sourcemap_upload_step = _sentry_js_sourcemaps_upload_step(
            sentry_sourcemaps, release_var=f"((.:{version_var}))"
        )

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
                    # Some Dockerfiles (e.g. ol-analytics-api) declare ARG GIT_SHA
                    # instead of the GIT_REF convention above; pass both so either
                    # naming picks up the git ref. Docker ignores unused build args.
                    "BUILD_ARG_GIT_SHA": "((.:git_ref))",
                    **version_args,
                    **additional_build_params,
                    **sourcemap_build_params,
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

    plan.append(_ensure_ecr_repository_step(ecr_registry_image_resource))
    plan.append(PutStep(put=dockerhub_registry_image_resource.name, params=put_params))
    plan.append(PutStep(put=ecr_registry_image_resource.name, params=put_params))

    if sourcemap_upload_step is not None:
        plan.append(sourcemap_upload_step)

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def _build_legacy_app_pipeline(
    app_name: str, pipeline_parameters: AppPipelineParams
) -> Pipeline:
    """Generate the legacy release-candidate/release-branch pipeline for an app."""
    (
        main_repo,
        release_candidate_repo,
        release_repo,
        ol_infra_repo,
    ) = _define_git_resources_legacy(
        app_name=app_name,
        repo_name=pipeline_parameters.repo_name,
        repo_main_branch=pipeline_parameters.repo_main_branch,
        repo_rc_branch=pipeline_parameters.repo_rc_branch,
        repo_release_branch=pipeline_parameters.repo_release_branch,
        ol_infra_branch=pipeline_parameters.ol_infra_branch,
    )
    (
        docker_ci_image,
        docker_rc_image,
        app_ci_image,
        app_rc_image,
    ) = _define_registry_image_resources_legacy(app_name)
    pulumi_resource_type, pulumi_resource = _define_pulumi_resources(
        app_name, ol_infra_repo.name
    )

    fastly_rtype: ResourceType | None = None
    fastly_ci: Resource | None = None
    fastly_qa: Resource | None = None
    fastly_prod: Resource | None = None

    if pipeline_parameters.purge_fastly_cache and pipeline_parameters.fastly_domains:
        fastly_rtype, fastly_ci, fastly_qa, fastly_prod = _define_fastly_resources(
            app_name=app_name,
            fastly_domains=pipeline_parameters.fastly_domains,
        )

    main_image_build_job = _build_image_job_legacy(
        app_name=app_name,
        branch_type="main",
        dockerfile_path=pipeline_parameters.dockerfile_path,
        git_repo_resource=main_repo,
        dockerhub_registry_image_resource=docker_ci_image,
        ecr_registry_image_resource=app_ci_image,
        build_target=pipeline_parameters.build_target,
        django_settings_dir=pipeline_parameters.settings_dir or "main",
        repo_version_file=pipeline_parameters.version_file,
        sentry_sourcemaps=pipeline_parameters.sentry_sourcemaps,
    )
    rc_image_build_job = _build_image_job_legacy(
        app_name=app_name,
        branch_type="release_candidate",
        dockerfile_path=pipeline_parameters.dockerfile_path,
        git_repo_resource=release_candidate_repo,
        dockerhub_registry_image_resource=docker_rc_image,
        ecr_registry_image_resource=app_rc_image,
        build_target=pipeline_parameters.build_target,
        django_settings_dir=pipeline_parameters.settings_dir or "main",
        repo_version_file=pipeline_parameters.version_file,
        sentry_sourcemaps=pipeline_parameters.sentry_sourcemaps,
    )

    ci_post_steps: list[GetStep | PutStep | TaskStep] = []
    if fastly_ci is not None:
        ci_post_steps.append(
            PutStep(
                put=fastly_ci.name,
                params=_fastly_purge_params(pipeline_parameters.fastly_purge_scope),
                no_get=True,
            )
        )

    # CI Deployment -- skipped entirely for apps with no CI Pulumi stack (e.g. a
    # hard runtime dependency is unavailable on the CI cluster). The main-branch
    # image is still built and published above regardless.
    ci_fragment = (
        pulumi_job(
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
        if pipeline_parameters.enable_ci_deploy
        else PipelineFragment(resource_types=[], resources=[], jobs=[])
    )

    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep]] = {}
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
        slack_url_path="eks.slack_url",
    )

    # Trigger a production deploy when the release branch is updated
    qa_and_production_fragment.jobs[-1].plan.insert(
        0, GetStep(get=release_repo.name, trigger=True)
    )

    # Make the release-candidate branch code available to the RC
    # pulumi deployment job similar to how it is available to production
    qa_and_production_fragment.jobs[0].plan.insert(
        0, GetStep(get=release_candidate_repo.name, trigger=False)
    )

    main_branch_container_fragement = PipelineFragment(
        resources=[main_repo, app_ci_image, docker_ci_image],
        jobs=[main_image_build_job],
    )

    release_candidate_container_fragment = PipelineFragment(
        resources=[release_candidate_repo, app_rc_image, docker_rc_image],
        jobs=[rc_image_build_job],
    )

    deployment_resources = [
        ol_infra_repo,
        pulumi_resource,
        release_repo,
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

    combined_fragment = PipelineFragment.combine_fragments(
        ci_deployment_fragment,
        qa_and_production_fragment,
        main_branch_container_fragement,
        release_candidate_container_fragment,
    )
    return combined_fragment.to_pipeline()


# ============================================================================
# Modernized workflow: GitHub Release resource + GitHub Deployments.
# Opt in per-app via AppPipelineParams.use_release_resource_workflow.
# ============================================================================


def _define_git_resources(
    app_name: str,
    repo_name: str | None,
    repo_main_branch: str,
    ol_infra_branch: str,
) -> tuple[Resource, Resource]:
    """Define the git resources needed for the release-resource pipeline."""
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
    repo_main_branch: str,
) -> tuple[Resource, Resource, Resource, Resource, Resource]:
    """Define the release-flow resources: release resource, gates, issues, and GitHub Deployments."""
    release_res = release_resource(
        name=Identifier(f"{app_name}-release"),
        uri=f"https://github.com/{github_repo}",
        branch=repo_main_branch,
        access_token="((github.release_resource_access_token))",  # noqa: S106
        repository=github_repo,
        semver_tag_fallback=True,
    )
    # Closed release issues gate production deployments.
    release_gate = github_issues(
        name=Identifier(f"{app_name}-release-gate"),
        repository=github_repo,
        issue_prefix=f"Release {app_name}",
        issue_title_template=f"Release {app_name}",
        issue_state="closed",
        skip_if_labeled=["abandoned"],
        access_token="((github.release_resource_access_token))",  # noqa: S106
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
        access_token="((github.release_resource_access_token))",  # noqa: S106
        gh_host=None,
    )
    deployment_rc = github_deployment(
        name=Identifier(f"{app_name}-deployment-rc"),
        repository=github_repo,
        environment="RC",
        access_token="((github.release_resource_access_token))",  # noqa: S106
    )
    deployment_prod = github_deployment(
        name=Identifier(f"{app_name}-deployment-production"),
        repository=github_repo,
        environment="Production",
        access_token="((github.release_resource_access_token))",  # noqa: S106
    )
    return release_res, release_gate, release_issue, deployment_rc, deployment_prod


def _define_registry_image_resources(
    app_name: str,
) -> tuple[Resource, Resource, Resource, Resource]:
    """Define the registry image resources needed for the release-resource pipeline."""
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
        tag_regex=r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+",  # calver YYYY.M.D.N e.g. 2026.6.15.1
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
        tag_regex=r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+",  # calver YYYY.M.D.N e.g. 2026.6.15.1
        sort_by_creation=True,
        **ecr_kwargs,
    )
    return docker_ci_image, docker_rc_image, ecr_ci_image, ecr_rc_image


def _build_image_job(
    app_name: str,
    dockerfile_path: str,
    git_repo_resource: Resource,
    dockerhub_registry_image_resource: Resource,
    ecr_registry_image_resource: Resource,
    build_target: str | None = None,
) -> Job:
    """Generate an image build job triggered by the configured git resource.

    This is the main-branch/CI build; its image only serves CI, so it uploads no
    source maps (see _build_release_image_job, whose image serves QA/Production).
    """
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
                # Some Dockerfiles (e.g. ol-analytics-api) declare ARG GIT_SHA
                # instead of the GIT_REF convention above; pass both so either
                # naming picks up the git ref. Docker ignores unused build args.
                "BUILD_ARG_GIT_SHA": "((.:git_ref))",
                "PROGRESS": "plain",
                **additional_build_params,
            },
            build_args=[],
        ),
        _ensure_ecr_repository_step(ecr_registry_image_resource),
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
    sentry_sourcemaps: SentrySourcemapsConfig | None = None,
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

    # UNPACK_ROOTFS lets the upload step read the maps out of the built image.
    sourcemap_build_params = {"UNPACK_ROOTFS": "true"} if sentry_sourcemaps else {}

    put_params: dict[str, Any] = {
        "image": "image/image.tar",
        # Use the version file as additional_tags so the calver tag is pushed
        # without triggering the registry-image resource's semver validation
        # (which rejects 4-part calver strings like 2026.6.15.1).
        # The primary push tag is source.tag ("latest"); the calver version is
        # pushed as an extra tag that downstream deployment jobs reference.
        "additional_tags": f"{release_res.name}/version",
    }

    plan = [
        GetStep(get=release_res.name, trigger=True),
        GetStep(get=main_repo.name, trigger=False),
        LoadVarStep(
            load_var="release_version",
            file=f"{release_res.name}/version",
            reveal=True,
        ),
        # The release resource's "create" out-action tags the pre-bumpver HEAD SHA
        # as the release (see ol-concourse resources/release/README.md): it records
        # main_repo's current HEAD *before* running bump_version_task, then commits
        # the version bump separately. Capture git_ref from main_repo here -- before
        # bump_version_task mutates the checkout -- so the built image is stamped
        # with the exact commit the release tag points to. The release resource
        # itself never writes a .git/ref file (only version/commits.json/
        # checklist.md/changelog_entry.md), so loading it from there would fail.
        LoadVarStep(
            load_var="git_ref",
            file=f"{main_repo.name}/.git/ref",
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
        container_build_task(
            inputs=[Input(name=main_repo.name)],
            build_parameters={
                "CONTEXT": main_repo.name,
                "DOCKERFILE": f"{main_repo.name}/{dockerfile_path}",
                "BUILD_ARG_GIT_REF": "((.:git_ref))",
                # Some Dockerfiles (e.g. ol-analytics-api) declare ARG GIT_SHA
                # instead of the GIT_REF convention above; pass both so either
                # naming picks up the git ref. Docker ignores unused build args.
                "BUILD_ARG_GIT_SHA": "((.:git_ref))",
                "BUILD_ARG_RELEASE_VERSION": "((.:release_version))",
                "PROGRESS": "plain",
                **additional_build_params,
                **sourcemap_build_params,
            },
            build_args=[],
        ),
        _ensure_ecr_repository_step(ecr_registry_image_resource),
        PutStep(put=dockerhub_registry_image_resource.name, params=put_params),
        PutStep(put=ecr_registry_image_resource.name, params=put_params),
    ]

    if sentry_sourcemaps:
        # Tag the artifacts with the release being built.
        plan.append(
            _sentry_js_sourcemaps_upload_step(
                sentry_sourcemaps, release_var="((.:release_version))"
            )
        )

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def _build_abandon_release_job(
    app_name: str,
    main_repo: Resource,
    release_res: Resource,
) -> Job:
    """Generate a manually-triggered job that abandons an in-flight release.

    Running this job deletes the ``releases/{version}`` branch and version tag
    from the remote so that the next ``check`` sees no in-flight release and
    recomputes the next version normally.  Use it when a release was cut but
    must be cancelled before it reaches production.
    """
    return Job(
        name=Identifier(f"abandon-{app_name}-release"),
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=release_res.name, trigger=False),
            GetStep(get=main_repo.name, trigger=False),
            PutStep(
                put=release_res.name,
                params={
                    "action": "abandon",
                    "repo_dir": str(main_repo.name),
                    "version_file": f"{release_res.name}/version",
                },
            ),
        ],
    )


def _build_release_resource_app_pipeline(
    app_name: str, pipeline_parameters: AppPipelineParams
) -> Pipeline:
    """Generate the modernized GitHub Release/Deployment-based pipeline for an app."""
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
        github_repo=pipeline_parameters.github_repo or f"mitodl/{app_name}",
        repo_main_branch=pipeline_parameters.repo_main_branch,
    )

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
        sentry_sourcemaps=pipeline_parameters.sentry_sourcemaps,
    )
    abandon_release_job = _build_abandon_release_job(
        app_name=app_name,
        main_repo=main_repo,
        release_res=release_res,
    )

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
        # title_template embeds the release version (via the image_tag var
        # loaded from release_res earlier in this job's plan) so each release
        # gets its own distinct issue title instead of every version
        # colliding on "Release {app_name}" -- Concourse resolves
        # ((.:image_tag)) to a plain string before the resource ever sees it.
        PutStep(
            put=release_issue.name,
            params={
                "body_file": f"{release_res.name}/checklist.md",
                "labels": ["release"],
                "title_template": f"Release {app_name} ((.:image_tag))",
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
        # Merge the release branch back into the main branch and delete it so
        # subsequent check calls no longer see a release as in-flight.
        PutStep(
            put=release_res.name,
            params={
                "action": "finish",
                "repo_dir": str(main_repo.name),
                "version_file": f"{release_res.name}/version",
            },
        ),
    ]
    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep]] = {
        0: qa_post_steps,
        1: prod_post_steps,
    }
    if fastly_qa is not None and fastly_prod is not None:
        purge_params = _fastly_purge_params(pipeline_parameters.fastly_purge_scope)
        additional_post_steps[0].append(
            PutStep(put=fastly_qa.name, params=purge_params, no_get=True)
        )
        additional_post_steps[1].append(
            PutStep(put=fastly_prod.name, params=purge_params, no_get=True)
        )

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
            # release_res carries the authoritative calver version; load it here
            # so image_tag is available to all jobs in the chain, including the
            # GitHub Deployment start steps and the Pulumi DOCKER_TAG env var.
            GetStep(
                get=release_res.name,
                trigger=False,
                passed=[release_image_build_job.name],
            ),
            LoadVarStep(
                load_var="image_tag", file=f"{release_res.name}/version", reveal=True
            ),
        ],
        # QA: get checklist.md from release resource; start RC GitHub Deployment.
        # Production: wait for release gate (closed release issue); start prod deployment.
        custom_dependencies={
            0: [
                # Concourse implicitly re-gets a resource right after a `put`
                # succeeds, so deployment.json (written by the get/`in` action)
                # is already available to the later action=finish put without
                # an explicit `get` here. An explicit `get` step for this
                # resource would count as a job input requiring the scheduler
                # to resolve a pre-existing version -- which can never happen
                # for a resource whose only versions come from this job's own
                # `put`, deadlocking the job forever on its first-ever run.
                PutStep(
                    put=deployment_rc.name,
                    params={"action": "start", "ref": "((.:image_tag))"},
                ),
            ],
            1: [
                GetStep(get=release_gate.name, trigger=True, version="every"),
                # main_repo is needed by the action=finish post-step.
                GetStep(
                    get=main_repo.name,
                    trigger=False,
                    passed=[release_image_build_job.name],
                ),
                # See the deployment_rc comment above: the implicit get after
                # this put already makes deployment.json available.
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
        jobs=[release_image_build_job, abandon_release_job],
    )

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

    combined_fragment = PipelineFragment.combine_fragments(
        ci_deployment_fragment,
        qa_and_production_fragment,
        main_branch_container_fragement,
        release_container_fragment,
    )
    return combined_fragment.to_pipeline()


def build_app_pipeline(app_name: str) -> Pipeline:
    """Generate the full Concourse pipeline for a given application.

    Dispatches to the modernized release-resource pipeline shape for apps that
    have opted in via ``AppPipelineParams.use_release_resource_workflow``, and
    to the legacy release-candidate/release-branch shape for everyone else.
    """
    pipeline_parameters = pipeline_params.get(app_name) or AppPipelineParams(
        app_name=app_name
    )
    if pipeline_parameters.use_release_resource_workflow:
        return _build_release_resource_app_pipeline(app_name, pipeline_parameters)
    return _build_legacy_app_pipeline(app_name, pipeline_parameters)


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
