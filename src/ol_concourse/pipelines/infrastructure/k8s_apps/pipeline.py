# ruff: noqa: PLR0913, E501
"""Generate Concourse pipeline definitions for building and deploying dockerized applications to Kubernetes via Pulumi."""

import sys
from pathlib import Path
from typing import Any

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import (
    PREVIEW_NO_CHANGES_MARKER,
    PREVIEW_SUMMARY_FILENAME,
    stack_serial_group,
)
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Duration,
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
from ol_concourse.lib.notifications import notification
from ol_concourse.lib.resource_types import (
    github_deployments_resource,
    github_issues_resource,
    pulumi_provisioner_resource,
    release_resource_type,
    slack_notification_resource,
)
from ol_concourse.lib.resources import (
    git_repo,
    github_deployment,
    github_issues,
    pulumi_provisioner,
    registry_image,
    release_resource,
    slack_notification,
)
from ol_concourse.lib.tasks import TASK_IMAGE, bump_version_task
from pydantic import BaseModel, model_validator

from bridge.settings.apps import PRODUCTION_TAG_PREFIX
from bridge.settings.apps import github_repo as app_github_repo
from bridge.settings.apps import release_changelog as app_release_changelog
from bridge.settings.apps import release_job_names as app_release_job_names
from bridge.settings.apps import release_kind as app_release_kind
from bridge.settings.apps import (
    release_resource_workflow as app_release_resource_workflow,
)
from bridge.settings.apps import repo_main_branch as app_repo_main_branch
from bridge.settings.sentry import MIT_LEARN_SENTRY_PROJECT_SLUG, SENTRY_ORG_SLUG
from ol_concourse.pipelines.applications.learn_ai_frontend.values import (
    ENVIRONMENTS as LEARN_AI_FRONTEND_ENVIRONMENTS,
)
from ol_concourse.pipelines.constants import (
    ECR_REGION,
    PULUMI_WATCHED_PATHS,
    dockerhub_ecr_image_uri,
)
from ol_concourse.pipelines.ecr import configure_ecr_repository_task
from ol_concourse.pipelines.infrastructure.k8s_apps.static_frontend import (
    StaticFrontendConfig,
    static_frontend_fragment,
)
from ol_concourse.pipelines.jobs import pulumi_job, pulumi_jobs_chain
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data
from ol_concourse.pipelines.secrets_map import project_secrets_paths
from ol_concourse.pipelines.versions_map import project_version_paths


class SentrySourcemapsConfig(BaseModel):
    """Config for uploading an app's source maps to Sentry from its pushed image.

    The maps are read out of the image the release build pushed, which the
    registry-image resource unpacks on ``get``, so the image build itself needs
    no Sentry credentials and no special output format. See
    :func:`_sourcemaps_upload_fragment`.

    Attributes:
        org: Sentry organization slug. Take it from ``bridge.settings.sentry``.
        project: Sentry project slug. Take it from ``bridge.settings.sentry``,
            where a test checks it against the Sentry Pulumi program: a stale
            slug makes ``sentry-cli`` fail with ``One or more projects are
            invalid`` on every upload.
        auth_token_vault_key: Concourse credential reference for the Sentry auth
            token (e.g. ``"((sentry.mitlearn_auth_token))"``). The token needs the
            ``org:read`` and ``project:releases`` scopes.
        rootfs_asset_path: Path, relative to the image's root filesystem, to the
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
        sentry_sourcemaps (Optional[SentrySourcemapsConfig]): When set, a separate
            job uploads the source maps out of each pushed release image to Sentry.
            It runs after the image build, nothing downstream waits on it, and it
            posts to Slack when it fails. The auth token is a task param of that
            job and never enters the image build. See :class:`SentrySourcemapsConfig`.
            Left unset, no source maps are uploaded.
        refresh_stack (bool): Whether the Pulumi deploy jobs run `pulumi refresh`
            before `up`. Defaults to True. Set False for apps whose Pulumi code
            provisions Fastly resources while the Fastly API token is being
            rotated -- refresh calls the Fastly API with the old token and fails
            the whole job.
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
    sentry_sourcemaps: SentrySourcemapsConfig | None = None
    refresh_stack: bool = True
    static_frontend: StaticFrontendConfig | None = None
    description: str = ""
    category: str = "applications"

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
        refresh_stack=False,
        description="Builds the MicroMasters Django app image and deploys it to Kubernetes via Pulumi, following the modernized GitHub Release/Deployment workflow.",
    ),
    "mitxonline": AppPipelineParams(
        app_name="mitxonline",
        build_target="production",
        refresh_stack=False,
        description="Builds the MITx Online Django app image and deploys it to Kubernetes via Pulumi, following the modernized GitHub Release/Deployment workflow.",
    ),
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
            org=SENTRY_ORG_SLUG,
            project=MIT_LEARN_SENTRY_PROJECT_SLUG,
            auth_token_vault_key="((sentry.mitlearn_auth_token))",  # noqa: S106  # pragma: allowlist secret
            rootfs_asset_path="app/frontends/main/.next",
        ),
        description="Builds the MIT Learn Next.js frontend image, deploys it to Kubernetes via Pulumi, purges the Fastly HTML-page cache on deploy, and uploads the build's source maps to Sentry.",
    ),
    "xpro": AppPipelineParams(
        app_name="xpro",
        repo_name=app_github_repo("xpro").split("/", 1)[1],
        repo_main_branch=app_repo_main_branch("xpro"),
        build_target="production",
        settings_dir="mitxpro",
        refresh_stack=False,
        description="Builds the xPRO Django app image and deploys it to Kubernetes via Pulumi, following the modernized GitHub Release/Deployment workflow.",
    ),
    "learn-ai": AppPipelineParams(
        app_name="learn-ai",
        build_target="production",
        refresh_stack=False,
        static_frontend=StaticFrontendConfig(
            path="frontend-demo", environments=LEARN_AI_FRONTEND_ENVIRONMENTS
        ),
        description="Builds the Learn AI service image and deploys it to Kubernetes via Pulumi, and ships its static frontend with each release, following the modernized GitHub Release/Deployment workflow.",
    ),
    "mit-learn": AppPipelineParams(
        app_name="mit-learn",
        build_target="production",
        refresh_stack=False,
        description="Builds the MIT Learn Django/API backend image and deploys it to Kubernetes via Pulumi, following the legacy release-candidate/release-branch workflow.",
    ),
    "ocw-studio": AppPipelineParams(
        app_name="ocw-studio",
        repo_main_branch=app_repo_main_branch("ocw-studio"),
        build_target="production",
        description="Builds the OCW Studio Django app image and deploys it to Kubernetes via Pulumi, following the modernized GitHub Release/Deployment workflow.",
    ),
    "odl-video-service": AppPipelineParams(
        app_name="odl-video-service",
        repo_main_branch=app_repo_main_branch("odl-video-service"),
        build_target="production",
        settings_dir="odl_video",
        description="Builds the ODL Video Service Django app image and deploys it to Kubernetes via Pulumi, following the modernized GitHub Release/Deployment workflow.",
    ),
    "ol-analytics-api": AppPipelineParams(
        app_name="ol-analytics-api",
        description="Builds the OL Analytics API image and deploys it to Kubernetes via Pulumi.",
        category="data-platform",
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


def _ensure_ecr_repository_step(
    ecr_registry_image_resource: Resource,
) -> list[TaskStep]:
    """Return steps to create the ECR repo if missing, then apply its
    scan-on-push + lifecycle-policy configuration.

    Without the second step, every build permanently accumulates as its own
    image in ECR -- Inspector re-scans and re-reports the same CVEs against
    every old, unused build forever. Other pipeline generators
    (witan, omnigraph, superset, ...) already pair ensure_ecr_task with
    configure_ecr_repository_task; this generator's apps (mit-learn,
    mitxonline, xpro, odl-video-service, and others sharing this step) were
    missing the second half of that pairing, confirmed live via
    `aws ecr describe-images` -- mit-learn-app alone had 1,204 accumulated
    images, each carrying its own full copy of the same ~1,900 CVEs.
    """
    repo_name = ecr_registry_image_resource.source["repository"]
    return [
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
                    "REPO_NAME": repo_name,
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
        # expire_after_days, not keep_last_n_images: this repo is shared by
        # independent CI (every main-branch commit) and release build jobs
        # (see the registry_image() calls above/below using the same
        # image_repository) -- a count-based policy would let CI churn
        # push the still-deployed release image out of the "N most
        # recent" window and delete it. Age-based means CI volume can't
        # threaten a release image redeployed within any reasonable
        # window. Flagged by Copilot review on PR #5728.
        configure_ecr_repository_task(repo_name, expire_after_days=90),
    ]


# ============================================================================
# Legacy workflow: release-candidate/release git-branch pattern.
# Used by every app whose bridge.settings.apps entry leaves
# release_resource_workflow False.
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
            *project_version_paths(f"applications/{app_name.replace('-', '_')}/"),
            *project_secrets_paths(f"applications/{app_name.replace('-', '_')}/"),
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


SOURCEMAPS_UPLOAD_TASK_NAME = "upload-sentry-sourcemaps"


def _sourcemaps_upload_fragment(
    app_name: str,
    sentry_config: SentrySourcemapsConfig,
    image_resource: Resource,
    image_build_job: str,
    release_version_file: str,
    extra_gets: list[GetStep],
) -> PipelineFragment:
    """Build the job that uploads a pushed release image's source maps to Sentry.

    The job triggers on ``image_resource`` once ``image_build_job`` has pushed
    it. The registry-image ``get`` unpacks that image's root filesystem into
    ``<image_resource>/rootfs``, so the maps uploaded are the ones inside the
    image that deploys. Reading them here rather than out of the build task's
    output is what lets the build use ``OUTPUT_OCI``: oci-build-task ignores
    ``UNPACK_ROOTFS`` for OCI output. For a multi-platform image the ``get``
    unpacks the variant matching the worker's OS and architecture.

    No deploy job lists this one in ``passed``, so a failed upload cannot hold
    a release. Running the upload inside the build job under a ``try`` looks
    like the simpler way to get that, but a ``try`` turns a failing upload into
    a green build, and nothing else reports that stack traces have stopped
    symbolicating. Here a failure is a red job plus a Slack alert, and the
    release goes on regardless.

    Sentry ties errors to source maps by Debug ID; ``--release`` only groups
    the artifact bundle under the release in the Sentry UI. The version comes
    from ``release_version_file``, read from one of the job's inputs.
    """
    slack_resource = slack_notification(
        name=Identifier(f"slack-alert-{app_name}-sourcemaps"),
        url="((eks.slack_url))",
    )
    rootfs_asset_dir = f"{image_resource.name}/rootfs/{sentry_config.rootfs_asset_path}"
    upload_task = TaskStep(
        task=Identifier(SOURCEMAPS_UPLOAD_TASK_NAME),
        # Sentry's API does fail transiently; a slug or token problem fails all
        # three times and the job goes red.
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
            inputs=[Input(name=image_resource.name)],
            params={"SENTRY_AUTH_TOKEN": sentry_config.auth_token_vault_key},
            run=Command(
                path="sentry-cli",
                args=[
                    "sourcemaps",
                    "upload",
                    "--org",
                    sentry_config.org,
                    "--project",
                    sentry_config.project,
                    "--release",
                    "((.:release_version))",
                    rootfs_asset_dir,
                ],
            ),
        ),
    )
    alert_body = (
        f"The {app_name} release image was pushed, but its source maps were not"
        f" uploaded to Sentry project {sentry_config.project}. Stack traces for"
        " this release stay minified until the upload job is rerun. The release"
        " itself is not blocked."
    )
    job = Job(
        name=Identifier(f"upload-{app_name}-sourcemaps"),
        build_log_retention={"builds": 10},
        plan=[
            # Every pushed image, not only the newest: a deploy job can select
            # image A while image B lands before this job schedules, and A
            # would then deploy with no maps and no alert. A job that has never
            # run starts at the newest passed build (Concourse's group resolver
            # falls through to the latest path when the job has no prior
            # build), so adding this job replays no history.
            GetStep(
                get=image_resource.name,
                trigger=True,
                version="every",
                passed=[image_build_job],
                params={"format": "rootfs"},
            ),
            *extra_gets,
            LoadVarStep(
                load_var="release_version", file=release_version_file, reveal=True
            ),
            upload_task,
        ],
        on_failure=notification(
            resource=slack_resource,
            title=f"{app_name} source maps did not reach Sentry",
            body=alert_body,
            alert_type="failed",
        ),
        on_error=notification(
            resource=slack_resource,
            title=f"{app_name} source map upload errored",
            body=alert_body,
            alert_type="errored",
        ),
    )
    return PipelineFragment(
        resource_types=[slack_notification_resource()],
        resources=[slack_resource],
        jobs=[job],
    )


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
        additional_build_params["TARGET"] = build_target
    # OUTPUT_OCI preserves the DHI-hardened base's zstd layer labeling
    # end-to-end (mitodl/ol-infrastructure#5714). oci-build-task's default
    # (Docker-legacy) output format has no field to declare a layer as
    # anything but gzip, so a layer carried through unchanged from a
    # zstd-compressed base gets silently mislabeled as gzip -- which is what
    # broke the original learn-ai canary build.
    additional_build_params["OUTPUT_OCI"] = "true"
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
                },
                build_args=[],
            ),
        ]
    )

    put_params: dict[str, Any] = {
        # A directory, not image.tar: oci-build-task's documented output shape
        # for OUTPUT_OCI.
        "image": "image/image",
        "additional_tags": f"./{git_repo_resource.name}/.git/short_ref",
    }
    if branch_type != "main":
        put_params["version"] = f"((.:{version_var}))"
        put_params["bump_aliases"] = True

    plan.extend(_ensure_ecr_repository_step(ecr_registry_image_resource))
    plan.append(PutStep(put=dockerhub_registry_image_resource.name, params=put_params))
    plan.append(PutStep(put=ecr_registry_image_resource.name, params=put_params))

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
    )
    # QA and Production both run the RC image, so uploading its maps covers
    # them; the main-branch image only serves CI. The RC image is pushed with
    # its version as the tag, which the get writes to ``<resource>/tag``.
    sourcemaps_fragments: list[PipelineFragment] = []
    if pipeline_parameters.sentry_sourcemaps is not None:
        sourcemaps_fragments.append(
            _sourcemaps_upload_fragment(
                app_name=app_name,
                sentry_config=pipeline_parameters.sentry_sourcemaps,
                image_resource=app_rc_image,
                image_build_job=rc_image_build_job.name,
                release_version_file=f"{app_rc_image.name}/tag",
                extra_gets=[],
            )
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
            refresh_stack=pipeline_parameters.refresh_stack,
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

    release_marker_slack = _release_marker_slack(app_name)
    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep | TryStep]] = {
        0: [],
        1: [_release_marker_task(app_name, ol_infra_repo, release_marker_slack)],
    }
    if fastly_qa is not None and fastly_prod is not None:
        purge_params = _fastly_purge_params(pipeline_parameters.fastly_purge_scope)
        additional_post_steps[0].append(
            PutStep(put=fastly_qa.name, params=purge_params, no_get=True)
        )
        additional_post_steps[1].append(
            PutStep(put=fastly_prod.name, params=purge_params, no_get=True)
        )

    qa_and_production_fragment = pulumi_jobs_chain(
        refresh_stack=pipeline_parameters.refresh_stack,
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
        custom_dependencies={1: [_release_marker_start_task()]},
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
        release_marker_slack,
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
        *sourcemaps_fragments,
    )
    return combined_fragment.to_pipeline()


# ============================================================================
# Modernized workflow: GitHub Release resource + GitHub Deployments.
# Opt in per-app via AppRegistration.release_resource_workflow in
# bridge.settings.apps -- the same field the release bot reads to decide
# whether it will accept commands for the app.
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
            *project_version_paths(f"applications/{app_name.replace('-', '_')}/"),
            *project_secrets_paths(f"applications/{app_name.replace('-', '_')}/"),
        ],
    )
    return main_repo, ol_infra_repo


# The release GitHub App; see the comment in _define_release_resources.
GITHUB_APP_ID = "((github_app.release_bot_app_id))"
GITHUB_APP_INSTALLATION_ID = "((github_app.release_bot_app_installation_id))"
GITHUB_APP_PRIVATE_KEY = "((github_app.release_bot_app_pem))"

# Release gate issue labels. Each records that something happened, so an issue
# collects them and nothing removes one; readers resolve them by precedence.
# The legacy unprefixed names are still read wherever a label is read, so
# renaming the labels in a repository never opens a window between the names.
RELEASE_LABEL = "ops:release"
CONSUMED_LABEL = "ops:consumed"
REFUSED_LABEL = "ops:refused"
ABANDONED_LABEL = "ops:abandoned"
ABANDONED_LABELS = [ABANDONED_LABEL, "abandoned"]

# The custom resource types the release flow keeps version history in, pinned
# to the ol-concourse commit each image was built from. Concourse keys a
# resource's history on its type's source and image, so while these tracked
# `latest`, every image publish gave each app's release resource an empty
# history on its next check. QA builds from before then no longer offer
# Production a matching set of inputs, so a release issue closed afterward
# deployed nothing until the next QA build, and Production then refused it
# when that build carried something newer
# (https://github.com/mitodl/mitxpro/issues/4172, 2026-11-03).
#
# Bumping a tag resets the same histories, once, on each pipeline's next check
# after k8s-apps-meta regenerates them, even when the image itself is
# unchanged: the type's source is what changed. Introducing these pins was
# such a bump. So bump deliberately, and afterwards check that each app with an
# open release issue still lists its release resource as an input of its latest
# QA build; where it does not, start a new QA build (fly rerun-build reuses the
# old input versions, which may no longer exist). The release bot reports an
# approved issue that Production has not taken.
RELEASE_RESOURCE_TYPE_TAGS = {
    "release": "326785450cb268077186cd220ea3b2d92b1ca918",  # pragma: allowlist secret
    "github-issues": "9ab0f9739aa61b39071be0022dcaec1bc8f0688c",  # pragma: allowlist secret
    "github-deployments": "efb28bc297f104f4bc8d63e33d07b114f710e2b3",  # pragma: allowlist secret
}


def _pin_release_resource_types(pipeline: Pipeline) -> Pipeline:
    """Set each RELEASE_RESOURCE_TYPE_TAGS type in *pipeline* to its pinned tag.

    Applied to the finished pipeline rather than to each fragment because the
    github-issues type arrives through the library's pulumi_jobs_chain, and
    fragment merging keeps whichever definition of a name comes first.
    """
    for resource_type in pipeline.resource_types or []:
        tag = RELEASE_RESOURCE_TYPE_TAGS.get(resource_type.name)
        if tag is None:
            continue
        source = resource_type.source
        if isinstance(source, RegistryImage):
            source.tag = tag
        elif isinstance(source, dict):
            source["tag"] = tag
        else:
            msg = f"resource type {resource_type.name!r} has no source to pin"
            raise TypeError(msg)
    return pipeline


def _define_release_resources(
    app_name: str,
    github_repo: str,
    repo_main_branch: str,
    *,
    generated_changelog: bool = True,
) -> tuple[Resource, Resource, Resource, Resource, Resource]:
    """Define the release-flow resources: release resource, gates, issues, and GitHub Deployments.

    ``generated_changelog`` has the release resource fold a Keep a Changelog
    entry into the ``Release <version>`` commit; see
    ``AppRegistration.release_changelog`` for when to turn it off.
    """
    # Shared GitHub App installation ol-concourse-lib no longer defaults --
    # every consumer of this library supplies its own credential reference.
    # Backed by the "shared/github_app" entry in
    # bridge/secrets/concourse/operations.production.yaml (Vault path
    # secret-concourse/shared/github_app), the same secret the release bot
    # reads. Deliberately not named "github" -- every Concourse team already
    # has its own team-scoped "github" secret (e.g. secret-concourse/
    # infrastructure/github), and Concourse's Vault credential manager tries
    # team-scoped paths before the shared fallback, so a credential named
    # plain "github" here would silently resolve against the wrong
    # (team-scoped, fieldless) secret instead of ever reaching this one.
    github_app_id = GITHUB_APP_ID
    github_app_installation_id = GITHUB_APP_INSTALLATION_ID
    github_app_private_key = GITHUB_APP_PRIVATE_KEY
    release_res = release_resource(
        name=Identifier(f"{app_name}-release"),
        uri=f"https://github.com/{github_repo}",
        branch=repo_main_branch,
        auth_method="app",
        app_id=github_app_id,
        app_installation_id=github_app_installation_id,
        private_ssh_key=github_app_private_key,
        repository=github_repo,
        semver_tag_fallback=True,
        changelog_style="cumulative" if generated_changelog else None,
    ).model_copy(
        # Not the library's `never`. Rebuilding the release resource image
        # gives this resource a fresh, empty version history on its next
        # check or put (Concourse keys history on the custom type's version),
        # and QA/Production take it with `passed:` the release build, which
        # resolves only against the current history. The resource's check
        # re-emits the newest cuts to repair that, but a put can start the
        # fresh history too, and under `never` nothing would check it until
        # the next `/doof release`. Concourse's periodic checker skips a
        # `never` resource outright, and otherwise checks a resource that
        # only triggers through `passed:`, like this one, only while its
        # history has never been checked, has no versions, or last failed to
        # check. So this costs a check after a fresh history starts and an
        # hourly retry while checks fail, not a check every hour.
        # A check cannot bypass the release build: QA triggers on this
        # resource (pulumi_jobs_chain turns the first stack's dependencies
        # into triggers) but only through `passed:` the release build, so it
        # runs only a version that build produced. A re-emitted cut is
        # normally a version QA has already run, so it starts nothing. The
        # exception: when the newest cut is gone or its build
        # failed, QA falls back to the cut before it and runs it if it never
        # had, deploying a release that passed the build to QA.
        update={"check_every": Duration("1h")}
    )
    # Closed release issues gate production deployments.
    release_gate = github_issues(
        name=Identifier(f"{app_name}-release-gate"),
        repository=github_repo,
        issue_prefix=f"Release {app_name}",
        issue_title_template=f"Release {app_name}",
        issue_state="closed",
        skip_if_labeled=ABANDONED_LABELS,
        auth_method="app",
        app_id=github_app_id,
        app_installation_id=github_app_installation_id,
        private_ssh_key=github_app_private_key,
        gh_host=None,
        # Production binds the newest ol-infrastructure revision that passed
        # QA when it starts, and refuses one its gate issue did not approve.
        # Every minute between a close and the check that records it is a
        # minute in which an ol-infrastructure merge reaching QA strands the
        # promotion. The release bot checks right after it closes an issue,
        # but that check can miss the close, and a close made on GitHub gets
        # no forced check at all. The default 60m left mitxonline 2026.10.28.1
        # waiting an hour and then refused.
        poll_frequency=Duration("5m"),
    )
    # The gate's get labels the issue it consumes instead of renaming it
    # `[CONSUMED #n]...`, so the issue keeps the version in its title. The
    # release bot reads the label; check skips an issue carrying it.
    release_gate.source["consume_with_label"] = CONSUMED_LABEL
    # Open release issues are created after each QA deployment.
    release_issue = github_issues(
        name=Identifier(f"{app_name}-release-issue"),
        repository=github_repo,
        issue_prefix=f"Release {app_name}",
        issue_title_template=f"Release {app_name}",
        issue_state="open",
        labels=[RELEASE_LABEL],
        auth_method="app",
        app_id=github_app_id,
        app_installation_id=github_app_installation_id,
        private_ssh_key=github_app_private_key,
        gh_host=None,
        # A retriggered QA deploy with no new app commit re-runs this put
        # with the same checklist; editing in place (instead of the default
        # comment-per-hit behavior) avoids reopening the issue with a fresh,
        # all-unchecked checklist for a release that hasn't actually changed.
        update_in_place=True,
    )
    # The abandon job's close_with_labels runs on this resource and labels
    # only the newest issue a gate consumed, which it finds by this label.
    release_issue.source["consume_with_label"] = CONSUMED_LABEL
    deployment_rc = github_deployment(
        name=Identifier(f"{app_name}-deployment-rc"),
        repository=github_repo,
        environment="RC",
        auth_method="app",
        app_id=github_app_id,
        app_installation_id=github_app_installation_id,
        private_ssh_key=github_app_private_key,
    )
    deployment_prod = github_deployment(
        name=Identifier(f"{app_name}-deployment-production"),
        repository=github_repo,
        environment="Production",
        auth_method="app",
        app_id=github_app_id,
        app_installation_id=github_app_installation_id,
        private_ssh_key=github_app_private_key,
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

    # OUTPUT_OCI keeps zstd layers inherited from the DHI-hardened base
    # labeled as zstd; see _build_image_job_legacy and
    # mitodl/ol-infrastructure#5714. This job uploads no source maps, so it
    # never needs the legacy tarball output.
    additional_build_params = {"OUTPUT_OCI": "true"}
    if build_target:
        additional_build_params["TARGET"] = build_target

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
        *_ensure_ecr_repository_step(ecr_registry_image_resource),
        PutStep(
            put=dockerhub_registry_image_resource.name,
            params={
                "image": "image/image",
                "additional_tags": f"./{git_repo_resource.name}/.git/short_ref",
            },
        ),
        PutStep(
            put=ecr_registry_image_resource.name,
            params={
                "image": "image/image",
                "additional_tags": f"./{git_repo_resource.name}/.git/short_ref",
            },
        ),
    ]

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def _checkout_release_task(main_repo: Resource, output: Identifier) -> TaskStep:
    """Check the cut `releases/<version>` branch out into *output*.

    Also records the commit the version tag points at in
    ``<output>/.git/release_ref``, which is what the image is stamped with.
    The app repositories are public, as is ``main_repo``'s unauthenticated
    https URI, so the fetch needs no credentials.
    """
    script = "\n".join(
        [
            f'cp -a "$REPO/." {output}/',
            f"cd {output}",
            "git fetch --quiet origin"
            ' "+refs/heads/releases/$VERSION:refs/remotes/origin/releases/$VERSION"'
            ' "+refs/tags/$VERSION:refs/tags/$VERSION"',
            'git checkout --quiet --force --detach "origin/releases/$VERSION"',
            "git clean --quiet -fdx",
            'git rev-list -n1 "$VERSION" > .git/release_ref',
        ]
    )
    return TaskStep(
        task=Identifier("checkout-release"),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            inputs=[Input(name=main_repo.name)],
            outputs=[Output(name=output)],
            params={"REPO": str(main_repo.name), "VERSION": "((.:release_version))"},
            run=Command(path="sh", args=["-euc", script]),
        ),
    )


# Inlined into the Production job's check task, so the generated pipeline
# carries the script itself rather than fetching it at run time. See the
# script's docstring for why the check exists.
_ASSERT_GATE_SCRIPT = (
    Path(__file__).parent / "scripts" / "assert_gate_names_deploy.py"
).read_text()
ASSERT_GATE_TASK_NAME = "assert-gate-names-deploy"
_LABEL_GATE_SCRIPT = (
    Path(__file__).parent / "scripts" / "label_gate_issue.py"
).read_text()
LABEL_GATE_TASK_NAME = "label-gate-issue"
# assert-gate-names-deploy writes `refused` here only when it refuses the issue.
GATE_REFUSAL_DIR = "gate-refusal"


def _label_gate_issue_task(
    github_repo: str, release_gate: Resource, labels: list[str]
) -> TaskStep:
    """Add *labels* to the issue the gate get took, if the check refused it.

    It never removes a label. A check that failed without refusing (GitHub
    unreachable) wrote no marker, so the issue is left alone.
    """
    return TaskStep(
        task=Identifier(LABEL_GATE_TASK_NAME),
        attempts=3,
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            inputs=[
                Input(name=release_gate.name),
                Input(name=Identifier(GATE_REFUSAL_DIR)),
            ],
            params={
                "GATE_FILE": f"{release_gate.name}/gh_issue.json",
                "ONLY_IF_FILE": f"{GATE_REFUSAL_DIR}/refused",
                "ADD_LABELS": ",".join(labels),
                "GITHUB_REPOSITORY": github_repo,
                "GITHUB_APP_ID": GITHUB_APP_ID,
                "GITHUB_APP_INSTALLATION_ID": GITHUB_APP_INSTALLATION_ID,
                "GITHUB_APP_PRIVATE_KEY": GITHUB_APP_PRIVATE_KEY,
            },
            run=Command(path="python3", args=["-c", _LABEL_GATE_SCRIPT]),
        ),
    )


def _assert_gate_names_deploy_task(
    app_name: str,
    github_repo: str,
    release_gate: Resource,
    version_input: Resource,
    infra_input: Identifier | None,
) -> TaskStep:
    """Fail the job unless the closed gate issue names what it deploys.

    *infra_input* None is a tag-only release, which deploys no
    ol-infrastructure code: the issue then approves the version alone.

    Accepts ``Release <app> <version>`` and ``Release <app> infrastructure @
    <version>``, and requires ``<version>`` to equal the contents of
    ``<version_input>/version``, which has to be the same file the job's
    ``image_tag`` comes from. The issue body's revision marker must also name
    the ol-infrastructure commit in ``<infra_input>``, which has to resolve to
    the same commit the job's Pulumi code get does. The gate get has already
    labelled the issue ``ops:consumed``, so a refused issue does not fire again. A failure
    here means nothing deployed, which turns gate/deploy drift into a red build
    instead of an unapproved release.

    On a refusal the issue is labelled ``ops:refused``, so the refusal shows
    on the issue and the release bot can report it, not only as a red build.
    The hook is on this task, not the job, so a failed deploy after the check
    passed does not label the issue refused, and it labels only when the
    check wrote its refusal marker, so a check that could not reach GitHub
    does not either.
    """
    return TaskStep(
        task=Identifier(ASSERT_GATE_TASK_NAME),
        on_failure=_label_gate_issue_task(github_repo, release_gate, [REFUSED_LABEL]),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            inputs=[
                Input(name=release_gate.name),
                Input(name=version_input.name),
                *([Input(name=infra_input)] if infra_input else []),
            ],
            # Concourse hands a failed task's outputs to its on_failure hook.
            outputs=[Output(name=Identifier(GATE_REFUSAL_DIR))],
            params={
                "APP_NAME": app_name,
                "REFUSAL_DIR": GATE_REFUSAL_DIR,
                "GATE_FILE": f"{release_gate.name}/gh_issue.json",
                "VERSION_FILE": f"{version_input.name}/version",
                **(
                    {"INFRA_REF_FILE": f"{infra_input}/.git/ref"} if infra_input else {}
                ),
                "GITHUB_REPOSITORY": github_repo,
                "GITHUB_APP_ID": GITHUB_APP_ID,
                "GITHUB_APP_INSTALLATION_ID": GITHUB_APP_INSTALLATION_ID,
                "GITHUB_APP_PRIVATE_KEY": GITHUB_APP_PRIVATE_KEY,
            },
            run=Command(path="python3", args=["-c", _ASSERT_GATE_SCRIPT]),
        ),
    )


_CLASSIFY_SCRIPT = (
    Path(__file__).parent / "scripts" / "classify_promotion.py"
).read_text()
CLASSIFY_TASK_NAME = "classify-promotion"
PROMOTION_DIR = "promotion"
CACHE_BUST_TASK_NAME = "cache-bust"
CACHE_BUST_DIR = "cache-bust"


def _cache_bust_task() -> TaskStep:
    """Write a value that differs on every build, to defeat Concourse's get cache.

    Concourse caches a ``get`` by resource, version and params, and what
    Production is running is not part of the release resource's version, so a
    repeated get of the same release would return the Production version it
    first saw. A get that passes this value in ``cache_bust`` runs ``in`` again.
    """
    return TaskStep(
        task=Identifier(CACHE_BUST_TASK_NAME),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            outputs=[Output(name=Identifier(CACHE_BUST_DIR))],
            params={"OUTPUT_DIR": CACHE_BUST_DIR},
            run=Command(
                path="sh",
                args=["-euc", 'date +%s%N > "$OUTPUT_DIR/value"'],
            ),
        ),
    )


_RELEASE_MARKER_SCRIPT = (
    Path(__file__).parent / "scripts" / "grafana_release_marker.py"
).read_text()
RELEASE_MARKER_START_TASK_NAME = "release-marker-start"
RELEASE_MARKER_TASK_NAME = "grafana-release-marker"
RELEASE_MARKER_DIR = "release-marker"
# Written to Vault by the grafana-alerting Production stack (release_markers.py).
GRAFANA_ANNOTATIONS_URL = "((grafana_annotations.url))"
GRAFANA_ANNOTATIONS_TOKEN = "((grafana_annotations.token))"  # noqa: S105


def _release_marker_start_task() -> TryStep:
    """Record when the Production rollout starts, for the release marker.

    The last dependency of the Production job, so the time is taken just
    before the Pulumi step that rolls the pods. Wrapped in a ``try`` so that
    failing to pull the task image cannot block a deploy; the marker step then
    errors on its missing ``release-marker`` input, inside its own ``try``, and
    alerts Slack.
    """
    return TryStep(
        try_=TaskStep(
            task=Identifier(RELEASE_MARKER_START_TASK_NAME),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=TASK_IMAGE,
                outputs=[Output(name=Identifier(RELEASE_MARKER_DIR))],
                params={"OUTPUT_DIR": RELEASE_MARKER_DIR},
                run=Command(
                    path="sh",
                    args=["-euc", 'date +%s > "$OUTPUT_DIR/started_at"'],
                ),
            ),
        )
    )


def _release_marker_slack(app_name: str) -> Resource:
    """Return the Slack resource the marker step alerts when it fails or errors."""
    return slack_notification(
        name=Identifier(f"slack-alert-{app_name}-release-marker"),
        url="((eks.slack_url))",
    )


def _release_marker_task(
    app_name: str, ol_infra_repo: Resource, slack_resource: Resource
) -> TryStep:
    """Mark a Production release on Grafana dashboards. See the script's docstring.

    The first Production post-step: Pulumi has waited for the rollout, so the
    marker spans it, and a later post-step that fails (the release finish, the
    Fastly purge) cannot cost a live release its marker. Wrapped in a ``try``
    because the release has shipped by the time this runs, and a red
    Production job would read as a failed deploy.

    The ``try`` also turns a failed or errored marker step green, so the
    task's own hooks alert Slack; nothing else would show that the Grafana
    token or the script has stopped working. Concourse runs a step's hooks
    outside its ``attempts``, so the alert is sent once, after the last attempt:
    https://github.com/concourse/concourse/blob/880fbe243731d36cf2573aa956b1acda89f20642/atc/steps.go#L223-L257
    """
    alert_body = (
        f"{app_name} ((.:image_tag)) rolled out to Production, and the rollout"
        f" needs nothing. The {RELEASE_MARKER_TASK_NAME} step did not succeed, so"
        " Grafana dashboards will not show this release if the version is new;"
        " a redeploy of an already-marked version loses nothing. The step's log"
        " says why: a 401 or 403 points at the Grafana token or its roles (the"
        " grafana-alerting Production stack, release_markers.py), and a missing"
        f" {RELEASE_MARKER_DIR} input points at the"
        f" {RELEASE_MARKER_START_TASK_NAME} step."
    )

    def alert(outcome: str) -> PutStep:
        step = notification(
            resource=slack_resource,
            title=(
                f"{app_name} deployed to Production;"
                f" Grafana release marker step {outcome}"
            ),
            body=alert_body,
            alert_type=outcome,
        )
        # Nothing reads the put's implicit get, and if that get errored after
        # on_failure had posted, on_error would post a second alert.
        step.no_get = True
        return step

    return TryStep(
        try_=TaskStep(
            task=Identifier(RELEASE_MARKER_TASK_NAME),
            attempts=3,
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=TASK_IMAGE,
                inputs=[
                    Input(name=Identifier(RELEASE_MARKER_DIR)),
                    Input(name=ol_infra_repo.name),
                ],
                params={
                    "APP_NAME": app_name,
                    "ENVIRONMENT": "production",
                    "VERSION": "((.:image_tag))",
                    "STARTED_AT_FILE": f"{RELEASE_MARKER_DIR}/started_at",
                    "INFRA_REF_FILE": f"{ol_infra_repo.name}/.git/ref",
                    "INFRA_REPO_URL": ol_infra_repo.source["uri"],
                    "GRAFANA_URL": GRAFANA_ANNOTATIONS_URL,
                    "GRAFANA_TOKEN": GRAFANA_ANNOTATIONS_TOKEN,
                },
                run=Command(path="python3", args=["-c", _RELEASE_MARKER_SCRIPT]),
            ),
            on_failure=alert("failed"),
            on_error=alert("errored"),
        )
    )


def _classify_promotion_task(
    app_name: str,
    release_res: Resource,
    current_release: Identifier,
    pulumi_resource: Resource | None,
    pulumi_code: Resource | None,
) -> TaskStep:
    """Compose the release issue's title and body from the Production preview.

    *pulumi_code* is the ol-infrastructure checkout the preview ran from; its
    commit goes into the body for the Production job to check. Both are None
    for a tag-only release, which has no preview: the body is the checklist.
    """
    preview_inputs: list[Input] = []
    preview_params: dict[str, str] = {}
    if pulumi_resource is not None and pulumi_code is not None:
        preview_inputs = [
            Input(name=pulumi_resource.name),
            Input(name=pulumi_code.name),
        ]
        preview_params = {
            "PREVIEW_SUMMARY_FILE": (
                f"{pulumi_resource.name}/{PREVIEW_SUMMARY_FILENAME}"
            ),
            "NO_CHANGES_MARKER_FILE": (
                f"{pulumi_resource.name}/{PREVIEW_NO_CHANGES_MARKER}"
            ),
            "INFRA_REF_FILE": f"{pulumi_code.name}/.git/ref",
            "INFRA_REPO_URL": pulumi_code.source["uri"],
        }
    return TaskStep(
        task=Identifier(CLASSIFY_TASK_NAME),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            inputs=[
                Input(name=release_res.name),
                Input(name=current_release),
                *preview_inputs,
            ],
            outputs=[Output(name=Identifier(PROMOTION_DIR))],
            params={
                "APP_NAME": app_name,
                "VERSION_FILE": f"{release_res.name}/version",
                "PRODUCTION_VERSION_FILE": f"{current_release}/production_version",
                "PRODUCTION_STATE_FILE": f"{current_release}/production_state",
                "CHECKLIST_FILE": f"{release_res.name}/checklist.md",
                **preview_params,
                "OUTPUT_DIR": PROMOTION_DIR,
            },
            run=Command(path="python3", args=["-c", _CLASSIFY_SCRIPT]),
        ),
    )


_SUPERSEDE_SCRIPT = (
    Path(__file__).parent / "scripts" / "supersede_gate_issues.py"
).read_text()
SUPERSEDE_TASK_NAME = "supersede-gate-issues"


def _supersede_gate_issues_task(
    app_name: str, github_repo: str, release_issue: Resource
) -> TaskStep:
    """Close, and label ``ops:abandoned``, every open gate issue but the one just posted.

    Runs after the release issue put, so the comment on each superseded issue
    can name the issue that replaced it. See the script's docstring for which
    issues count. Retried, because it is what keeps a stale issue from being
    closed by mistake, and not wrapped in a ``try``: a failure is red.
    """
    return TaskStep(
        task=Identifier(SUPERSEDE_TASK_NAME),
        attempts=3,
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            inputs=[
                Input(name=Identifier(PROMOTION_DIR)),
                Input(name=release_issue.name),
            ],
            params={
                "APP_NAME": app_name,
                "GITHUB_REPOSITORY": github_repo,
                "PROMOTION_DIR": PROMOTION_DIR,
                "ISSUE_FILE": f"{release_issue.name}/gh_issue.json",
                "GITHUB_APP_ID": GITHUB_APP_ID,
                "GITHUB_APP_INSTALLATION_ID": GITHUB_APP_INSTALLATION_ID,
                "GITHUB_APP_PRIVATE_KEY": GITHUB_APP_PRIVATE_KEY,
            },
            run=Command(path="python3", args=["-c", _SUPERSEDE_SCRIPT]),
        ),
    )


def _preview_production_step(
    pulumi_resource: Resource,
    pulumi_code: Resource,
    docker_sha_env: str,
    release_version_env: str,
    *,
    refresh_stack: bool,
) -> PutStep:
    """Preview the Production stack at the image digest it would deploy.

    Fail-closed, unlike the retired ``preview_next_stack``: the preview is the
    content of the gate issue, so one that fails fails the job and no issue is
    posted. It takes the Production stack lock, which is why the QA and
    Production jobs share a serial group.
    """
    return PutStep(
        put=pulumi_resource.name,
        inputs="all",
        no_get=False,
        get_params={
            "summary_file": PREVIEW_SUMMARY_FILENAME,
            "read_outputs": False,
            "preview_stack": "Production",
        },
        params={
            "env_os": {
                "AWS_DEFAULT_REGION": "us-east-1",
                "PYTHONPATH": f"/usr/lib/:/tmp/build/put/{pulumi_code.name}/src/",
                "GITHUB_TOKEN": "((github.public_repo_access_token))",
                docker_sha_env: "((.:image_digest))",
                release_version_env: "((.:image_tag))",
            },
            "stack_name": "Production",
            "preview": True,
            "env_vars_from_files": {},
            **({"refresh_stack": False} if not refresh_stack else {}),
        },
    )


def _build_release_image_job(
    app_name: str,
    dockerfile_path: str,
    main_repo: Resource,
    release_res: Resource,
    dockerhub_registry_image_resource: Resource,
    ecr_registry_image_resource: Resource,
    build_target: str | None = None,
) -> Job:
    """Generate the release image build job for an app.

    Nothing schedules this job: the release bot checks the release resource
    and then triggers the job explicitly, and a hotfix takes the same path
    with the commit to cherry-pick carried in the resource's ``hotfix`` file,
    since triggering a job carries no parameters.  Checking the resource does
    not start a build, and neither does a `put` to it -- see the comment on
    the first get for why that matters.

    This job:
    1. Gets the release resource and main repo source.
    2. Bumps the version in the app source using bumpver.
    3. Creates the release commit, branch, and tag via the release resource.
    4. Builds and pushes a versioned Docker image to DockerHub and ECR.
    """
    job_name = f"build-{app_name}-release-image"

    additional_build_params = {}
    if build_target:
        additional_build_params = {"TARGET": build_target}
    # OUTPUT_OCI keeps zstd layers inherited from the DHI-hardened base
    # labeled as zstd; see _build_image_job_legacy and
    # mitodl/ol-infrastructure#5714.
    additional_build_params["OUTPUT_OCI"] = "true"

    put_params: dict[str, Any] = {
        # A directory, not image.tar: oci-build-task's output shape for OUTPUT_OCI.
        "image": "image/image",
        # Use the version file as additional_tags so the calver tag is pushed
        # without triggering the registry-image resource's semver validation
        # (which rejects 4-part calver strings like 2026.6.15.1).
        # The primary push tag is source.tag ("latest"); the calver version is
        # pushed as an extra tag that downstream deployment jobs reference.
        "additional_tags": f"{release_res.name}/version",
    }

    release_source = Identifier("release-source")
    plan = [
        # Deliberately not `trigger: true`. A `put` publishes a version of the
        # resource, and Concourse schedules on a version it has not seen
        # before whether that version came from a check or from a put. This
        # job's own `action: create` put, the `action: finish` put at the end
        # of deploy-production, and the `action: abandon` put therefore all
        # re-triggered this job, each carrying the version just released:
        #
        #   create   the cut no-ops against the matching tag, but the image is
        #            still rebuilt and re-pushed, and a digest differing from
        #            the first build carries it back through the deploy chain
        #   finish   fails, because the release branch has been merged back and
        #            the commit a re-cut would tag is now the merge commit
        #            ("Tag X already exists at <sha>, which does not match the
        #            commit being released")
        #   abandon  deletes the branch and the version tag but leaves an
        #            ``abandoned/<version>`` tombstone, which ``create``
        #            refuses, so the re-cut fails instead of resurrecting the
        #            abandoned release
        #
        # Nothing is lost by dropping the trigger. Concourse checks the
        # resource on its own only to repair a fresh version history (see
        # _define_release_resources), there is no webhook, and the release bot
        # starts a release by checking the resource over the API and then
        # triggering this job explicitly (see release_bot.bot._release), so
        # the trigger could only ever fire on a put, on that repair, or race
        # the bot's own build.
        GetStep(get=release_res.name, trigger=False),
        GetStep(get=main_repo.name, trigger=False),
        LoadVarStep(
            load_var="release_version",
            file=f"{release_res.name}/version",
            reveal=True,
        ),
        # The commits a hotfix cherry-picks onto production, one SHA or
        # several separated by whitespace, or empty for a normal release. The
        # release resource's check sets it while `hotfix/<sha>` request tags
        # are pending, which is how `/doof hotfix` gets SHAs to a job whose
        # trigger carries no parameters. `commit_hash` takes the whole string,
        # so the number of commits does not change this plan.
        LoadVarStep(
            load_var="hotfix",
            file=f"{release_res.name}/hotfix",
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
                "commit_hash": "((.:hotfix))",
            },
        ),
        # Build from the cut release, not from main_repo. A hotfix is
        # production plus its commits, which main_repo does not hold, and
        # Concourse does not document a put writing back to its inputs, so the
        # put's checkout is not something later steps can rely on. For a normal
        # release, releases/<version> is the tree main_repo holds after
        # bump_version_task, unless main moved between the get and the put, in
        # which case the branch is what was actually tagged.
        _checkout_release_task(main_repo, release_source),
        # The commit the version tag points at: the pre-bump HEAD for a normal
        # release, the last cherry-picked commit for a hotfix.
        LoadVarStep(
            load_var="git_ref",
            file=f"{release_source}/.git/release_ref",
            reveal=True,
        ),
        container_build_task(
            inputs=[Input(name=release_source)],
            build_parameters={
                "CONTEXT": str(release_source),
                "DOCKERFILE": f"{release_source}/{dockerfile_path}",
                "BUILD_ARG_GIT_REF": "((.:git_ref))",
                # Some Dockerfiles (e.g. ol-analytics-api) declare ARG GIT_SHA
                # instead of the GIT_REF convention above; pass both so either
                # naming picks up the git ref. Docker ignores unused build args.
                "BUILD_ARG_GIT_SHA": "((.:git_ref))",
                "BUILD_ARG_RELEASE_VERSION": "((.:release_version))",
                "PROGRESS": "plain",
                **additional_build_params,
            },
            build_args=[],
        ),
        *_ensure_ecr_repository_step(ecr_registry_image_resource),
        PutStep(put=dockerhub_registry_image_resource.name, params=put_params),
        PutStep(put=ecr_registry_image_resource.name, params=put_params),
    ]

    return Job(name=Identifier(job_name), build_log_retention={"builds": 10}, plan=plan)


def _build_abandon_release_job(
    app_name: str,
    main_repo: Resource,
    release_res: Resource,
    release_issue: Resource,
    serial_groups: list[str] | None = None,
) -> Job:
    """Generate a manually-triggered job that abandons an in-flight release.

    Running this job deletes the ``releases/{version}`` branch and version tag
    from the remote and leaves an ``abandoned/<version>`` tombstone, so the
    next ``check`` sees no in-flight release and skips the abandoned number
    when it computes the next version.  Use it when a release was cut but
    must be cancelled before it reaches production.

    It then labels the release's gate issue ``ops:abandoned`` and closes it. That
    label is what ``release_gate``'s ``skip_if_labeled`` looks for; without
    it the open issue outlives the release it names, and closing it later
    would deploy a version whose branch and tag are gone.  The issue step runs
    only after the release put succeeds, so a refused abandon (a version that
    is not in flight) leaves its issue alone.

    *serial_groups* puts the job in the release's serial group, as the
    tag-only pipeline does; see _build_tag_release_pipeline.
    """
    return Job(
        name=Identifier(f"abandon-{app_name}-release"),
        build_log_retention={"builds": 10},
        serial_groups=serial_groups,
        plan=[
            GetStep(get=release_res.name, trigger=False),
            GetStep(get=main_repo.name, trigger=False),
            LoadVarStep(
                load_var="abandoned_version",
                file=f"{release_res.name}/version",
                reveal=True,
            ),
            PutStep(
                put=release_res.name,
                params={
                    "action": "abandon",
                    "repo_dir": str(main_repo.name),
                    "version_file": f"{release_res.name}/version",
                },
                no_get=True,
            ),
            PutStep(
                put=release_issue.name,
                params={
                    "title_template": f"Release {app_name} ((.:abandoned_version))",
                    "close_with_labels": [ABANDONED_LABEL],
                },
                no_get=True,
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

    github_repo = pipeline_parameters.github_repo or f"mitodl/{app_name}"
    (
        release_res,
        release_gate,
        release_issue,
        deployment_rc,
        deployment_prod,
    ) = _define_release_resources(
        app_name=app_name,
        github_repo=github_repo,
        repo_main_branch=pipeline_parameters.repo_main_branch,
        generated_changelog=app_release_changelog(app_name),
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
    )
    # The version comes from the release resource at the build that pushed the
    # image, the same way the QA deploy job reads it.
    sourcemaps_fragments: list[PipelineFragment] = []
    if pipeline_parameters.sentry_sourcemaps is not None:
        sourcemaps_fragments.append(
            _sourcemaps_upload_fragment(
                app_name=app_name,
                sentry_config=pipeline_parameters.sentry_sourcemaps,
                image_resource=app_rc_image,
                image_build_job=release_image_build_job.name,
                release_version_file=f"{release_res.name}/version",
                extra_gets=[
                    GetStep(
                        get=release_res.name,
                        trigger=False,
                        passed=[release_image_build_job.name],
                    )
                ],
            )
        )
    abandon_release_job = _build_abandon_release_job(
        app_name=app_name,
        main_repo=main_repo,
        release_res=release_res,
        release_issue=release_issue,
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
        refresh_stack=pipeline_parameters.refresh_stack,
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
    project_name = f"ol-application-{app_name}"
    docker_sha_env = f"{app_name.replace('-', '_').upper()}_DOCKER_SHA"
    # The digest does not say which release it is, so the calver travels
    # alongside it and becomes the pods' ol.mit.edu/version label.
    release_version_env = f"{app_name.replace('-', '_').upper()}_RELEASE_VERSION"
    release_current = Identifier(f"{app_name}-release-current")
    # The ol-infra commit Production deploys, fetched again under its own name
    # so the gate check can read it before anything deploys: pulumi_job puts
    # its own code get after every dependency. Both gets take ol-infra with
    # `passed:` QA, as do the release and image gets. Concourse pins inputs
    # that share a `passed:` job to the outputs of one build of that job and
    # rejects any build whose version differs, so the two gets name the same
    # commit.
    infra_revision = Identifier(f"{ol_infra_repo.name}-revision")
    qa_job_name = f"deploy-{project_name}-qa"
    qa_post_steps: list[GetStep | PutStep | TaskStep | TryStep] = [
        # Preview Production at the image it would deploy, then fold that diff
        # and the app checklist into one release issue, so a single approval
        # covers everything the Production deploy would change. See
        # _preview_production_step: this fails the job rather than posting a
        # gate that does not show the diff.
        _preview_production_step(
            pulumi_resource,
            ol_infra_repo,
            docker_sha_env,
            release_version_env,
            refresh_stack=pipeline_parameters.refresh_stack,
        ),
        # What Production is running has to be current, not whatever the first
        # get of this release version wrote: see _cache_bust_task.
        _cache_bust_task(),
        LoadVarStep(
            load_var="cache_bust",
            file=f"{CACHE_BUST_DIR}/value",
            reveal=True,
        ),
        # `passed:` the release build, like the release get above. Concourse
        # records a job's inputs by resource, not by step name, and Production
        # takes the release `passed:` this job, so whatever version this get
        # resolves to is one Production can deploy. Unconstrained, it took the
        # resource's latest check, which is the next, uncut release once the
        # main branch moves past the last one, and Production resolved to it
        # (micromasters, 2026-10-27). The files read from it, production_version
        # and production_state, come from the GitHub Deployments API on every
        # `in` and do not depend on the version, so a cut version serves.
        # Do not instead add the release build to Production's `passed:`: an
        # input whose `passed:` names two jobs bound the Production job to an
        # older QA build than the newest (mitodl/ol-infrastructure#6685).
        GetStep(
            get=release_current,
            resource=release_res.name,
            passed=[release_image_build_job.name],
            params={"cache_bust": "((.:cache_bust))"},
        ),
        _classify_promotion_task(
            app_name, release_res, release_current, pulumi_resource, ol_infra_repo
        ),
        # Open (or update) the GitHub Release Issue. The title names the
        # version it ships, or the live version for an infrastructure-only
        # promotion, so each gets its own issue instead of every one colliding
        # on "Release {app_name}" -- Concourse resolves ((.:promotion_title))
        # to a plain string before the resource ever sees it. The skip marker
        # is written when there is nothing to approve, and the put then posts
        # nothing at all rather than closing an issue that approves something.
        #
        # release_tag is the version for a new release and empty for an
        # infrastructure-only one. Until a newer release is built, this job
        # still carries the last cut version, including an abandoned one, and
        # the put searches only open issues, so it would open a gate issue for
        # a release whose branch and tag are gone. The tag rather than the
        # branch, which a finished release has lost too: see
        # classify_promotion.py.
        LoadVarStep(
            load_var="promotion_title",
            file=f"{PROMOTION_DIR}/title",
            reveal=True,
        ),
        LoadVarStep(
            load_var="release_tag",
            file=f"{PROMOTION_DIR}/release_tag",
            reveal=True,
        ),
        PutStep(
            put=release_issue.name,
            params={
                "body_file": f"{PROMOTION_DIR}/body.md",
                "labels": [RELEASE_LABEL],
                "title_template": "((.:promotion_title))",
                "skip_if_file": f"{PROMOTION_DIR}/skip",
                "skip_if_tag_missing": "((.:release_tag))",
                # A QA re-run after the issue was approved opens a new issue
                # for the same release; keep the ticks the approval had,
                # unless the release was abandoned and this is its re-cut.
                "carry_checks_unless_labeled": ABANDONED_LABELS,
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
    release_marker_slack = _release_marker_slack(app_name)
    prod_post_steps: list[GetStep | PutStep | TaskStep | TryStep] = [
        _release_marker_task(app_name, ol_infra_repo, release_marker_slack),
        # Mark the Production GitHub Deployment as successful, and say so on the
        # release issue. Closing that issue authorizes the promotion; it does
        # not show the release is live, and a closed issue is easy to misread as
        # "shipped". The comment links the Deployment, which is what records
        # what is live. The gate's get already put gh_issue.json in the job.
        PutStep(
            put=deployment_prod.name,
            params={
                "action": "finish",
                "deployment_file": f"{deployment_prod.name}/deployment.json",
                "state": "success",
                "comment_on_issue_file": f"{release_gate.name}/gh_issue.json",
            },
        ),
        # Merge the release branch back into the main branch and delete it so
        # subsequent check calls no longer see a release as in-flight.
        #
        # Deliberately NOT wrapped in a `try`. It used to be, because this job
        # can be retriggered by an infra-only merge (QA re-runs on any change
        # under the watched Pulumi paths and opens a fresh release_gate issue
        # for the same version once the earlier one is closed; closing that
        # one is a new trigger for Concourse's version="every" get here) with
        # no new app release -- and finish then failed trying to fetch a release branch that a prior
        # finish had already deleted. The release resource now no-ops in that
        # case instead of failing, so the `try` buys nothing and costs
        # everything: it equally swallowed *genuine* finish failures. That is
        # how ol-analytics-api sat with an unmerged releases/2026.8.3.1 branch
        # -- and therefore a frozen release version -- from 2026-08-03 onward,
        # with no red build to show for it. A failure here now means a real
        # release that never finished, and should stop the job.
        PutStep(
            put=release_res.name,
            params={
                "action": "finish",
                "repo_dir": str(main_repo.name),
                "version_file": f"{release_res.name}/version",
            },
        ),
    ]
    additional_post_steps: dict[int, list[GetStep | PutStep | TaskStep | TryStep]] = {
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
    # Last in QA, after the RC deployment is marked and the cache purged, so
    # a GitHub failure here leaves the job red without misreporting either.
    additional_post_steps[0].append(
        _supersede_gate_issues_task(app_name, github_repo, release_issue)
    )

    # QA and Production Deployments
    qa_and_production_fragment = pulumi_jobs_chain(
        refresh_stack=pipeline_parameters.refresh_stack,
        pulumi_code=ol_infra_repo,
        stack_names=["QA", "Production"],
        project_name=project_name,
        project_source_path=(
            f"src/ol_infrastructure/applications/{app_name.replace('-', '_')}"
        ),
        # The QA job previews Production, which takes the Production stack
        # lock; lock recovery will not cancel a lock under 15 minutes old, so
        # without a shared group a live preview would fail a real deploy.
        serial_groups={
            0: [
                stack_serial_group(project_name, "QA"),
                stack_serial_group(project_name, "Production"),
            ],
            1: [stack_serial_group(project_name, "Production")],
        },
        additional_post_steps=additional_post_steps,
        dependencies=[
            GetStep(
                get=app_rc_image.name,
                trigger=True,
                passed=[release_image_build_job.name],
                params={"skip_download": True},
            ),
            # release_res carries the authoritative calver version; load it here
            # so image_tag is available to all jobs in the chain, for the
            # GitHub Deployment refs, the gate check and the pods' version label.
            GetStep(
                get=release_res.name,
                trigger=False,
                passed=[release_image_build_job.name],
            ),
            LoadVarStep(
                load_var="image_tag", file=f"{release_res.name}/version", reveal=True
            ),
            # Deploy by digest, not by the calver tag. A tag is mutable: an
            # image re-pushed under the same tag leaves a tag-pinned pod spec
            # unchanged, so nothing rolls out and RC keeps serving the old
            # build while every job reports success. (A re-cut after an
            # abandon no longer reuses the version, because the abandoned/
            # tombstone makes the release resource skip it, but the digest
            # also survives a stale node image cache.) The image
            # get above is chained `passed:` QA in the Production job, so
            # Production deploys the digest QA ran. skip_download still writes
            # the digest file.
            LoadVarStep(
                load_var="image_digest",
                file=f"{app_rc_image.name}/digest",
                reveal=True,
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
                GetStep(
                    get=infra_revision,
                    resource=ol_infra_repo.name,
                    trigger=False,
                    passed=[qa_job_name],
                ),
                # Before the deployment record or the deploy itself: the
                # issue that fired must name the version and the ol-infra
                # commit being deployed.
                _assert_gate_names_deploy_task(
                    app_name, github_repo, release_gate, release_res, infra_revision
                ),
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
                _release_marker_start_task(),
            ],
        },
        additional_env_vars={
            docker_sha_env: "((.:image_digest))",
            release_version_env: "((.:image_tag))",
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
        release_marker_slack,
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

    fragments = [
        ci_deployment_fragment,
        qa_and_production_fragment,
        main_branch_container_fragement,
        release_container_fragment,
        *sourcemaps_fragments,
    ]
    if pipeline_parameters.static_frontend is not None:
        fragments.append(
            static_frontend_fragment(
                app_name=app_name,
                repo_uri=f"https://github.com/{github_repo}",
                repo_main_branch=pipeline_parameters.repo_main_branch,
                config=pipeline_parameters.static_frontend,
                deployment_rc=deployment_rc,
                deployment_prod=deployment_prod,
                qa_job_name=qa_job_name,
                production_job_name=f"deploy-{project_name}-production",
            )
        )
    return _pin_release_resource_types(
        PipelineFragment.combine_fragments(*fragments).to_pipeline()
    )


# ============================================================================
# Tag-only release pipeline: a release that deploys nothing itself
# ============================================================================
# The repository's consumers check it out at a tag (ocw-hugo-themes and
# ocw-hugo-projects, read by ocw-studio's site pipelines). RC consumers track
# the newest release tag, which `action: create` pushes; Production consumers
# track the newest `production/<version>` tag, which only the Production job
# writes, after the gate issue is closed. Opt in with
# AppRegistration.release_kind="tag" in bridge.settings.apps.

_PROMOTE_TAG_SCRIPT = (Path(__file__).parent / "scripts" / "promote_tag.py").read_text()
PROMOTE_TAG_TASK_NAME = "promote-tag"
CHECK_PROMOTE_TAG_TASK_NAME = "check-promote-tag"


def _promote_tag_task(
    github_repo: str, release_res: Resource, *, check_only: bool = False
) -> TaskStep:
    """Create the release's ``production/<version>`` tag at its release tag.

    Retried: re-running finds the tag already at the commit and changes
    nothing. With *check_only* the task runs the script's checks (the release
    tag is there, no newer version has a Production tag, and this version's
    tag is absent or already at the commit) and creates nothing. See the
    script's docstring.
    """
    params = {
        "VERSION_FILE": f"{release_res.name}/version",
        "TAG_PREFIX": PRODUCTION_TAG_PREFIX,
        "GITHUB_REPOSITORY": github_repo,
        "GITHUB_APP_ID": GITHUB_APP_ID,
        "GITHUB_APP_INSTALLATION_ID": GITHUB_APP_INSTALLATION_ID,
        "GITHUB_APP_PRIVATE_KEY": GITHUB_APP_PRIVATE_KEY,
    }
    if check_only:
        params["CHECK_ONLY"] = "true"
    return TaskStep(
        task=Identifier(
            CHECK_PROMOTE_TAG_TASK_NAME if check_only else PROMOTE_TAG_TASK_NAME
        ),
        attempts=3,
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            inputs=[Input(name=release_res.name)],
            params=params,
            run=Command(path="python3", args=["-c", _PROMOTE_TAG_SCRIPT]),
        ),
    )


def _build_tag_release_pipeline(app_name: str) -> Pipeline:
    """Generate the release pipeline for a tag-only app.

    Four jobs, with the names ``bridge.settings.apps.release_job_names`` gives
    the release bot:

    - ``cut-<app>-release`` bumps the version and cuts the release, which
      pushes the release tag. Like ``build-<app>-release-image``, nothing
      schedules it: the bot checks the release resource and triggers it, a
      hotfix included (see ``_build_release_image_job``).
    - ``deploy-<app>-rc`` records the RC GitHub Deployment and opens the
      checklist gate issue. RC consumers pick the tag up themselves.
    - ``promote-<app>-production`` runs when the gate issue is closed, checks
      the issue names the version and was opened by the release App, creates
      the ``production/<version>`` tag, records the Production Deployment, and
      merges the release branch back.
    - ``abandon-<app>-release`` is the k8s apps' abandon job.

    All four jobs share a serial group. A promotion never finishes a release
    while the RC job is reading what Production runs, and a cut (which
    supersedes the in-flight release) or an abandon never deletes the release
    tag between promote-tag reading it and creating the Production tag. Those
    two keep a release's tag only once its Production Deployment has
    succeeded, and that is recorded after the tag is created.

    The Production job refuses a version older than the newest Production
    tag before it starts the Production Deployment, so re-running an old
    build cannot report that version as live while consumers run the newer
    tag.

    Before the first release the repository needs what the k8s apps have: a
    ``[tool.bumpversion]`` table in ``pyproject.toml`` (bump_version_task reads
    that file, even in an npm repository) naming the files to bump, such as a
    ``VERSION`` file. Without it the cut job fails at bump-version and nothing
    is tagged.
    """
    jobs = app_release_job_names(app_name)
    github_repo = app_github_repo(app_name)
    repo_main_branch = app_repo_main_branch(app_name)
    main_repo = git_repo(
        name=Identifier(f"{app_name}-{repo_main_branch}"),
        uri=f"https://github.com/{github_repo}",
        branch=repo_main_branch,
    )
    (
        release_res,
        release_gate,
        release_issue,
        deployment_rc,
        deployment_prod,
    ) = _define_release_resources(
        app_name=app_name,
        github_repo=github_repo,
        repo_main_branch=repo_main_branch,
        generated_changelog=app_release_changelog(app_name),
    )
    cut_job_name = jobs["release"]
    rc_job_name = jobs["qa"]
    serial_groups = [f"{app_name}-release"]
    release_current = Identifier(f"{app_name}-release-current")
    version_var = LoadVarStep(
        load_var="version", file=f"{release_res.name}/version", reveal=True
    )

    cut_job = Job(
        name=Identifier(cut_job_name),
        build_log_retention={"builds": 10},
        serial_groups=serial_groups,
        plan=[
            # Not `trigger: true`, for the reasons in _build_release_image_job.
            GetStep(get=release_res.name, trigger=False),
            GetStep(get=main_repo.name, trigger=False),
            LoadVarStep(
                load_var="hotfix", file=f"{release_res.name}/hotfix", reveal=True
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
                    "commit_hash": "((.:hotfix))",
                },
            ),
        ],
    )
    rc_job = Job(
        name=Identifier(rc_job_name),
        build_log_retention={"builds": 10},
        serial_groups=serial_groups,
        plan=[
            GetStep(get=release_res.name, trigger=True, passed=[cut_job_name]),
            version_var,
            # The implicit get after this put makes deployment.json available
            # to the finish put; see _build_release_resource_app_pipeline.
            PutStep(
                put=deployment_rc.name,
                params={"action": "start", "ref": "((.:version))"},
            ),
            # What Production runs has to be current; see _cache_bust_task and
            # the release_current get in _build_release_resource_app_pipeline.
            _cache_bust_task(),
            LoadVarStep(
                load_var="cache_bust", file=f"{CACHE_BUST_DIR}/value", reveal=True
            ),
            GetStep(
                get=release_current,
                resource=release_res.name,
                passed=[cut_job_name],
                params={"cache_bust": "((.:cache_bust))"},
            ),
            _classify_promotion_task(
                app_name, release_res, release_current, None, None
            ),
            LoadVarStep(
                load_var="promotion_title",
                file=f"{PROMOTION_DIR}/title",
                reveal=True,
            ),
            LoadVarStep(
                load_var="release_tag",
                file=f"{PROMOTION_DIR}/release_tag",
                reveal=True,
            ),
            PutStep(
                put=release_issue.name,
                params={
                    "body_file": f"{PROMOTION_DIR}/body.md",
                    "labels": [RELEASE_LABEL],
                    "title_template": "((.:promotion_title))",
                    "skip_if_file": f"{PROMOTION_DIR}/skip",
                    "skip_if_tag_missing": "((.:release_tag))",
                    "carry_checks_unless_labeled": ABANDONED_LABELS,
                },
            ),
            PutStep(
                put=deployment_rc.name,
                params={
                    "action": "finish",
                    "deployment_file": f"{deployment_rc.name}/deployment.json",
                    "state": "success",
                },
            ),
            _supersede_gate_issues_task(app_name, github_repo, release_issue),
        ],
    )
    production_job = Job(
        name=Identifier(jobs["production"]),
        build_log_retention={"builds": 10},
        serial_groups=serial_groups,
        plan=[
            GetStep(get=release_res.name, trigger=False, passed=[rc_job_name]),
            version_var,
            GetStep(get=release_gate.name, trigger=True, version="every"),
            _assert_gate_names_deploy_task(
                app_name, github_repo, release_gate, release_res, None
            ),
            # main_repo is needed by the action=finish put.
            GetStep(get=main_repo.name, trigger=False, passed=[cut_job_name]),
            # Before the Deployment starts, so a refused version records none.
            _promote_tag_task(github_repo, release_res, check_only=True),
            PutStep(
                put=deployment_prod.name,
                params={"action": "start", "ref": "((.:version))"},
            ),
            _promote_tag_task(github_repo, release_res),
            PutStep(
                put=deployment_prod.name,
                params={
                    "action": "finish",
                    "deployment_file": f"{deployment_prod.name}/deployment.json",
                    "state": "success",
                    "comment_on_issue_file": f"{release_gate.name}/gh_issue.json",
                },
            ),
            # Not in a `try`; see the finish put in
            # _build_release_resource_app_pipeline.
            PutStep(
                put=release_res.name,
                params={
                    "action": "finish",
                    "repo_dir": str(main_repo.name),
                    "version_file": f"{release_res.name}/version",
                },
            ),
        ],
    )
    abandon_job = _build_abandon_release_job(
        app_name=app_name,
        main_repo=main_repo,
        release_res=release_res,
        release_issue=release_issue,
        serial_groups=serial_groups,
    )
    return _pin_release_resource_types(
        Pipeline(
            resource_types=[
                release_resource_type(),
                github_deployments_resource(),
                github_issues_resource(),
            ],
            resources=[
                main_repo,
                release_res,
                release_gate,
                release_issue,
                deployment_rc,
                deployment_prod,
            ],
            jobs=[cut_job, rc_job, production_job, abandon_job],
        )
    )


def build_app_pipeline(app_name: str) -> Pipeline:
    """Generate the full Concourse pipeline for a given application.

    Dispatches to the modernized release-resource pipeline shape for apps that
    have opted in via ``AppRegistration.release_resource_workflow`` in
    ``bridge.settings.apps``, and to the legacy release-candidate/release-branch
    shape for everyone else. The opt-in lives in the shared registry rather than
    in ``pipeline_params`` because the release bot has to make the same call, and
    a per-app flag duplicated across the two would let a pipeline be migrated
    while the bot still refuses to drive it.
    """
    if app_release_kind(app_name) == "tag":
        return _build_tag_release_pipeline(app_name)
    pipeline_parameters = pipeline_params.get(app_name) or AppPipelineParams(
        app_name=app_name
    )
    if app_release_resource_workflow(app_name):
        return _build_release_resource_app_pipeline(app_name, pipeline_parameters)
    if pipeline_parameters.static_frontend is not None:
        # The frontend jobs chain off the release workflow's GitHub Deployments;
        # the legacy shape has none, and would silently drop the frontend.
        msg = (
            f"{app_name}: static_frontend requires release_resource_workflow=True "
            "in bridge.settings.apps."
        )
        raise ValueError(msg)
    return _build_legacy_app_pipeline(app_name, pipeline_parameters)


if __name__ == "__main__":
    import sys

    app_name = sys.argv[1] if len(sys.argv) > 1 else None
    if not app_name:
        msg = "Please provide an app name as a command line argument."
        raise ValueError(msg)
    built_pipeline = build_app_pipeline(app_name=app_name)
    app_params = pipeline_params.get(app_name)
    output = pipeline_json_with_user_data(
        built_pipeline,
        user_data={
            "description": (app_params.description if app_params else "")
            or f"Builds `{app_name}` and deploys it to Kubernetes via Pulumi.",
            "team": "infrastructure",
            "category": app_params.category if app_params else "applications",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    # Note: The pipeline name generated below might need adjustment
    # if the app_name changes the resulting pipeline identifier.
    pipeline_name = f"docker-pulumi-{app_name}"
    sys.stdout.writelines(
        (
            "\n",
            f"fly -t pr-inf sp -p {pipeline_name} -c definition.json",
        )
    )
