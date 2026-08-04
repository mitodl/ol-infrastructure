"""Concourse pipeline for building and deploying the learn-ai static frontend.

Recreates the three GitHub Actions workflows deleted in
https://github.com/mitodl/learn-ai/pull/13 -- ``ci-deploy.yml``,
``rc-deploy.yml`` and ``production-deploy.yml``.

One job per environment, each triggered by its own branch of
``mitodl/learn-ai``: ``main`` -> CI, ``release-candidate`` -> QA, ``release`` ->
Production.  Every environment rebuilds rather than promoting a shared
artifact, because the ``NEXT_PUBLIC_*`` values are baked into the static export
at build time and differ per environment.

Each job:

1. Builds ``frontend-demo/`` with Node 24 + Yarn 4, producing a Next.js static
   export in ``frontend-demo/out/`` (``next.config.ts`` sets ``output: "export"``).
2. Syncs that directory to ``s3://<bucket>/frontend/`` via rclone.  The
   ``/frontend/`` prefix is required -- the Fastly service in front of the bucket
   rewrites every request to live under it
   (``ol_infrastructure/applications/learn_ai/files/frontend_path_prefix.vcl``).
3. Purges the environment's Fastly cache.

S3 credentials come from the Concourse worker's instance role
(``rclone env_auth = true``).  The worker policy in
``src/ol_infrastructure/applications/concourse/iam_policies/operations.py``
must grant write access to ``ol-mit-learn-ai-*/frontend/*``.

Usage:

    python pipeline.py
    fly -t pr-inf sp -p learn-ai-frontend -c definition.json
"""

import sys
import textwrap

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import fastly_resource_type, rclone
from ol_concourse.lib.resources import fastly_service, git_repo

from ol_concourse.pipelines.applications.learn_ai_frontend.values import (
    ENVIRONMENTS,
    LearnAIFrontendEnv,
)
from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

LEARN_AI_URI = "https://github.com/mitodl/learn-ai"
FRONTEND_PATH = "frontend-demo/"
BUILD_OUTPUT = Identifier("site-dist")
BUCKET_RESOURCE = Identifier("learn-ai-frontend-bucket")

# The Fastly API token lives under the team-scoped `fastly` secret; the field is
# `fastly_api_token`, not the ol-concourse library default of `api_token`.
FASTLY_API_TOKEN = "((fastly.fastly_api_token))"  # noqa: S105


def _build_script(repo_input: Identifier) -> str:
    """Return the bash script that produces the static export.

    ``corepack enable`` is required: ``frontend-demo/package.json`` pins Yarn
    4.x via ``packageManager`` while the node image ships Yarn 1.  The actual
    Yarn download happens on the first ``yarn`` invocation -- see
    ``COREPACK_ENABLE_DOWNLOAD_PROMPT`` in the task params.  ``.git/ref`` is the
    git resource's commit SHA and stands in for ``$GITHUB_SHA`` in the
    ``hash.txt`` deploy marker the old workflows wrote.
    """
    return textwrap.dedent(
        f"""\
        set -euo pipefail
        REF="$(cat {repo_input}/.git/ref)"
        cd {repo_input}/{FRONTEND_PATH}
        corepack enable
        yarn install --immutable
        yarn build
        echo "${{REF}}" > out/hash.txt
        cp -a out/. ../../{BUILD_OUTPUT}/
        """
    )


def learn_ai_frontend_job(env: LearnAIFrontendEnv) -> PipelineFragment:
    """Build and deploy the learn-ai frontend for a single environment."""
    repo = git_repo(
        name=Identifier(f"learn-ai-{env.slug}"),
        uri=LEARN_AI_URI,
        branch=env.branch,
        paths=[FRONTEND_PATH],
    )
    fastly = fastly_service(
        name=Identifier(f"learn-ai-frontend-fastly-{env.slug}"),
        api_token=FASTLY_API_TOKEN,
        domain=env.fastly_domain,
        # Purge-only: do not trigger builds on VCL activations.
        check_every="never",
    )

    job = Job(
        name=Identifier(f"deploy-learn-ai-frontend-{env.slug}"),
        plan=[
            GetStep(get=repo.name, trigger=True),
            TaskStep(
                task=Identifier(f"build-learn-ai-frontend-{env.slug}"),
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
                    inputs=[Input(name=repo.name)],
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
                    run=Command(
                        path="bash",
                        args=["-c", _build_script(repo.name)],
                    ),
                ),
            ),
            # rclone `sync` is a destructive mirror, matching the `--delete` the
            # deleted s3-sync-action passed.
            PutStep(
                put=BUCKET_RESOURCE,
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
        ],
    )

    return PipelineFragment(resources=[repo, fastly], jobs=[job])


def learn_ai_frontend_pipeline() -> Pipeline:
    """Assemble the three per-environment deploy jobs into one pipeline."""
    combined = PipelineFragment.combine_fragments(
        *(learn_ai_frontend_job(env) for env in ENVIRONMENTS)
    )

    # A single rclone resource serves all three environments -- the target
    # bucket is supplied per `put` step via `destination.dir`.  Credentials come
    # from the Concourse worker instance role.
    bucket = Resource(
        name=BUCKET_RESOURCE,
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

    return Pipeline(
        resource_types=[rclone(), fastly_resource_type(), *combined.resource_types],
        resources=[bucket, *combined.resources],
        jobs=combined.jobs,
    )


if __name__ == "__main__":
    pipeline = learn_ai_frontend_pipeline()
    definition_json = pipeline.model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(definition_json)
    sys.stdout.write(definition_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p learn-ai-frontend -c definition.json")  # noqa: T201
