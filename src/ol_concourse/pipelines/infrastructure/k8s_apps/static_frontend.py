"""Static-site frontend jobs that ship with an app's release.

Some apps have a statically exported frontend (learn-ai's ``frontend-demo/``)
that is built in Concourse and synced to S3 behind Fastly rather than baked into
the app image. This module builds those jobs for the release-resource pipeline
so the frontend follows the backend release instead of watching branches:

* CI rebuilds whenever the frontend path changes on the main branch.
* QA runs after the app's QA deploy and builds the calver tag that deploy
  recorded in the RC GitHub Deployment.
* Production runs after the app's Production deploy and builds the tag recorded
  in the Production GitHub Deployment, so it can only ship what was approved at
  the release gate.

Both gated jobs build from the release tag, not from a branch. The release
resource tags the pre-bump HEAD, and the tag outlives ``releases/<version>``,
which ``action: finish`` deletes.
"""

import textwrap
from typing import Any

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
    Platform,
    PutStep,
    Resource,
    ResourceType,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import fastly_resource_type, rclone
from ol_concourse.lib.resources import fastly_service, git_repo
from ol_concourse.lib.tasks import TASK_IMAGE
from pydantic import BaseModel

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

BUILD_OUTPUT = Identifier("site-dist")
RELEASE_SOURCE = Identifier("frontend-release-source")

# The Fastly API token lives under the team-scoped `fastly` secret; the field is
# `fastly_api_token`, not the ol-concourse library default of `api_token`.
FASTLY_API_TOKEN = "((fastly.fastly_api_token))"  # noqa: S105


class StaticFrontendEnv(BaseModel):
    """One deploy target for an app's static frontend."""

    stage: str  # "CI" | "QA" | "Production"
    bucket: str  # S3 bucket created by the app's Pulumi stack
    fastly_domain: str  # hostname served by the environment's Fastly service
    build_vars: dict[str, str]  # NEXT_PUBLIC_* values baked into the export

    @property
    def slug(self) -> str:
        """Lowercased stage name, safe for use inside Concourse identifiers."""
        return self.stage.lower()


class StaticFrontendConfig(BaseModel):
    """A static frontend built from a directory of the app's repository."""

    path: str = "frontend-demo"
    environments: list[StaticFrontendEnv]


def _build_script(source: Identifier, path: str) -> str:
    """Return the bash script that produces the static export.

    ``corepack enable`` is required: the frontend's ``package.json`` pins Yarn
    4.x via ``packageManager`` while the node image ships Yarn 1.  The actual
    Yarn download happens on the first ``yarn`` invocation -- see
    ``COREPACK_ENABLE_DOWNLOAD_PROMPT`` in the task params.  ``.git/ref`` is the
    commit SHA of the source and stands in for ``$GITHUB_SHA`` in the
    ``hash.txt`` deploy marker the old GitHub Actions workflows wrote.
    """
    return textwrap.dedent(
        f"""\
        set -euo pipefail
        REF="$(cat {source}/.git/ref)"
        cd {source}/{path}
        corepack enable
        yarn install --immutable
        yarn build
        echo "${{REF}}" > out/hash.txt
        cp -a out/. ../../{BUILD_OUTPUT}/
        """
    )


def _checkout_tag_task(repo_uri: str, version: str) -> TaskStep:
    """Check the release tag *version* out into ``RELEASE_SOURCE``.

    The app repositories are public, so the clone needs no credentials. The
    commit goes in ``.git/ref`` to match what the git resource would have
    written for the CI job.
    """
    script = "\n".join(
        [
            "git -c advice.detachedHead=false clone --quiet --depth 1"
            f' --branch "$VERSION" "$REPO_URI" {RELEASE_SOURCE}',
            f"git -C {RELEASE_SOURCE} rev-parse HEAD > {RELEASE_SOURCE}/.git/ref",
        ]
    )
    return TaskStep(
        task=Identifier("checkout-frontend-release"),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=TASK_IMAGE,
            outputs=[Output(name=RELEASE_SOURCE)],
            params={"REPO_URI": repo_uri, "VERSION": version},
            run=Command(path="sh", args=["-euc", script]),
        ),
    )


def _deploy_steps(  # noqa: PLR0913
    app_name: str,
    env: StaticFrontendEnv,
    source: Identifier,
    path: str,
    bucket: Resource,
    fastly: Resource,
) -> list[TaskStep | PutStep]:
    """Build the export from *source*, sync it to the bucket and purge Fastly."""
    return [
        TaskStep(
            task=Identifier(f"build-{app_name}-frontend-{env.slug}"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={
                        "repository": dockerhub_ecr_image_uri("node"),
                        "tag": "24",
                        "aws_region": ECR_REGION,
                    },
                ),
                inputs=[Input(name=source)],
                outputs=[Output(name=BUILD_OUTPUT)],
                params={
                    "NODE_ENV": "production",
                    # Corepack prompts for confirmation before fetching the
                    # Yarn release pinned in package.json.  A Concourse task
                    # has no TTY, so the build hangs on that prompt forever
                    # without this.
                    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
                    **env.build_vars,
                },
                run=Command(path="bash", args=["-c", _build_script(source, path)]),
            ),
        ),
        # rclone `sync` is a destructive mirror, matching the `--delete` the
        # deleted s3-sync-action passed. The frontend sits under a /frontend/
        # key prefix that the Fastly service in front of the bucket expects.
        PutStep(
            put=bucket.name,
            params={
                "source": str(BUILD_OUTPUT),
                "destination": [
                    {
                        "command": "sync",
                        "dir": f"s3-remote:{env.bucket}/frontend/",
                    }
                ],
            },
        ),
        PutStep(put=fastly.name, params={"mode": "purge_all"}, no_get=True),
    ]


def static_frontend_fragment(  # noqa: PLR0913
    app_name: str,
    repo_uri: str,
    repo_main_branch: str,
    config: StaticFrontendConfig,
    deployment_rc: Resource,
    deployment_prod: Resource,
    qa_job_name: str,
    production_job_name: str,
) -> PipelineFragment:
    """Return the frontend jobs and resources for an app on the release workflow.

    ``qa_job_name`` and ``production_job_name`` are the app's own deploy jobs.
    Each gated frontend job takes the matching GitHub Deployment with
    ``passed:`` that job, so it runs only for a release whose backend deploy
    succeeded, and reads the release tag from the Deployment's ``ref``.
    """
    bucket = Resource(
        name=Identifier(f"{app_name}-frontend-bucket"),
        type="rclone",
        source={
            "config": textwrap.dedent(
                """\
                [s3-remote]
                type = s3
                provider = AWS
                env_auth = true
                region = us-east-1
                """
            )
        },
    )
    resources: list[Resource] = [bucket]
    resource_types: list[ResourceType] = [rclone(), fastly_resource_type()]
    jobs: list[Job] = []
    gated = {
        "QA": (deployment_rc, qa_job_name),
        "Production": (deployment_prod, production_job_name),
    }

    for env in config.environments:
        fastly = fastly_service(
            name=Identifier(f"{app_name}-frontend-fastly-{env.slug}"),
            api_token=FASTLY_API_TOKEN,
            domain=env.fastly_domain,
            # Purge-only: do not trigger builds on VCL activations.
            check_every="never",
        )
        resources.append(fastly)
        job_name = Identifier(f"deploy-{app_name}-frontend-{env.slug}")

        if env.stage in gated:
            deployment, deploy_job = gated[env.stage]
            plan: list[Any] = [
                GetStep(get=deployment.name, trigger=True, passed=[deploy_job]),
                LoadVarStep(
                    load_var="deployment",
                    file=f"{deployment.name}/deployment.json",
                    format="json",
                    reveal=True,
                ),
                _checkout_tag_task(repo_uri, "((.:deployment.ref))"),
                *_deploy_steps(
                    app_name, env, RELEASE_SOURCE, config.path, bucket, fastly
                ),
            ]
        else:
            source = git_repo(
                name=Identifier(f"{app_name}-frontend-{env.slug}"),
                uri=repo_uri,
                branch=repo_main_branch,
                paths=[config.path],
            )
            resources.append(source)
            plan = [
                GetStep(get=source.name, trigger=True),
                *_deploy_steps(app_name, env, source.name, config.path, bucket, fastly),
            ]
        # serial: every build ends in a destructive `rclone sync` to the same
        # bucket prefix. Concourse runs a job's builds in parallel by default,
        # so two overlapping builds (QA re-runs record a new Deployment each
        # time) could let an older tag finish last and overwrite a newer one.
        jobs.append(
            Job(
                name=job_name,
                serial=True,
                build_log_retention={"builds": 10},
                plan=plan,
            )
        )

    return PipelineFragment(
        resource_types=resource_types, resources=resources, jobs=jobs
    )
