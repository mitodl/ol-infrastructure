"""Concourse pipeline for the omnigraph-server service.

The ``omnigraph-server`` image build context lives in the ``mitodl/agent-kit``
repo (NOT ol-infrastructure): ``docker/omnigraph-server.Dockerfile`` builds
from the agent-kit repo ROOT so it can reach the whole uv workspace plus the
``schema.pg`` baked into the image. This is the crux that separates this
pipeline from every existing ol-infrastructure precedent — the build context
is a foreign repo — so it watches a dedicated ``agent-kit`` git resource for
the image and runs the Pulumi deploy off the ol-infrastructure checkout.

Flow (mirrors ``kubewatch``'s build-then-deploy shape):

    agent-kit push -> build omnigraph-server image -> push to omnigraph-server-<env> ECR
        -> pulumi deploy ``ol-application-omnigraph`` (CI -> QA -> Production),
           each stage gated (``passed``) on its freshly built image.

The ECR repositories (``omnigraph-server-<env>``) are provisioned by the
Pulumi stack itself (``applications/omnigraph``); this pipeline only builds the
image and pushes to them, exactly like ``kubewatch_webhook_handler``'s "ECR
repo in Pulumi, image built elsewhere" split.

witan (``pulumi-witan``) is a separate pipeline deploying a separate stack that
reaches this service via a StackReference; the two ship independently.

Fly command to bootstrap this pipeline (normally set by the
``pulumi-infrastructure-meta`` meta pipeline):
    python pipeline.py
    fly -t pr-inf sp -p pulumi-omnigraph -c definition.json
"""

import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import (
    ECR_REGION,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)
from ol_concourse.pipelines.jobs import pulumi_jobs_chain

ENVIRONMENTS = ("CI", "QA", "Production")
IMAGE_NAME = "omnigraph-server"
DOCKERFILE = "docker/omnigraph-server.Dockerfile"

PULUMI_PROJECT_PATH = PULUMI_CODE_PATH.joinpath("applications/omnigraph/")


def build_omnigraph_pipeline() -> PipelineFragment:
    """Build the image-build + Pulumi-deploy pipeline for omnigraph-server."""
    # Source for the image build: the agent-kit repo root (the Dockerfile
    # builds from root to reach the whole uv workspace + baked schema.pg).
    agent_kit_code = git_repo(
        name=Identifier("agent-kit"),
        uri="https://github.com/mitodl/agent-kit",
        branch="main",
        paths=[
            "docker/",
            "pyproject.toml",
            "uv.lock",
            "packages/",
            "mcp/servers/witan/",
            "mcp/servers/witan-code/",
        ],
    )

    # Source for the Pulumi deploy: the ol-infrastructure checkout.
    pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-omnigraph"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_PROJECT_PATH),
        ],
    )

    # One registry-image resource per env: the build job pushes the single
    # freshly built tarball to each env's ECR repo, and each deploy stage gates
    # on its own env resource.
    image_resources: dict[str, Resource] = {
        env: registry_image(
            name=Identifier(f"{IMAGE_NAME}-{env.lower()}-image"),
            image_repository=f"{IMAGE_NAME}-{env.lower()}",
            ecr_region=ECR_REGION,
        )
        for env in ENVIRONMENTS
    }

    build_job = Job(
        name=Identifier(f"build-{IMAGE_NAME}-image"),
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=agent_kit_code.name, trigger=True),
            container_build_task(
                inputs=[Input(name=agent_kit_code.name)],
                build_parameters={
                    "CONTEXT": agent_kit_code.name,
                    "DOCKERFILE": f"{agent_kit_code.name}/{DOCKERFILE}",
                },
            ),
            *[
                PutStep(
                    put=image_resources[env].name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"{agent_kit_code.name}/.git/short_ref",
                    },
                )
                for env in ENVIRONMENTS
            ],
        ],
    )

    # Each deploy stage triggers off — and is gated (``passed``) on — the image
    # for that env, so a stage only redeploys images this pipeline itself built.
    custom_dependencies: dict[int, list[GetStep]] = {
        idx: [
            GetStep(
                get=image_resources[env].name,
                trigger=True,
                passed=[build_job.name],
            )
        ]
        for idx, env in enumerate(ENVIRONMENTS)
    }

    deploy_fragment = pulumi_jobs_chain(
        refresh_stack=True,
        pulumi_code=pulumi_code,
        stack_names=list(ENVIRONMENTS),
        project_name="ol-application-omnigraph",
        project_source_path=PULUMI_PROJECT_PATH,
        custom_dependencies=custom_dependencies,
    )

    # combine_fragments deduplicates resources/resource-types by name (the
    # field validators only fire on assignment, not on ``.append``), so route
    # the final assembly through it rather than mutating a fragment in place.
    build_fragment = PipelineFragment(
        resources=[agent_kit_code, pulumi_code, *image_resources.values()],
        jobs=[build_job],
    )
    return PipelineFragment.combine_fragments(build_fragment, deploy_fragment)


if __name__ == "__main__":
    pipeline = build_omnigraph_pipeline().to_pipeline()

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p pulumi-omnigraph -c definition.json")
    )
