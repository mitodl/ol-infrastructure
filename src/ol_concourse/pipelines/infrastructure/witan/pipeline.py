"""Concourse pipeline for the witan MCP service.

The ``witan`` image build context lives in the ``mitodl/agent-kit`` repo (NOT
ol-infrastructure): ``docker/witan.Dockerfile`` builds from the agent-kit repo
ROOT so it can reach the whole uv workspace and the witan MCP servers. This is
the crux that separates this pipeline from every existing ol-infrastructure
precedent — the build context is a foreign repo — so it watches a dedicated
``agent-kit`` git resource for the image and runs the Pulumi deploy off the
ol-infrastructure checkout.

Flow (mirrors ``kubewatch``'s build-then-deploy shape):

    agent-kit push -> build witan image -> push to witan ECR
        -> pulumi deploy ``ol-application-witan`` CI, gated on that image
           -> pulumi deploy QA, gated (``passed``) on CI having deployed that
              same image -> Production, gated on QA. One image is built once
              and promoted unchanged through every stage, matching every
              other build+deploy pipeline in this repo (e.g. ``k8s_apps``).

The build job ensures the ECR repository (``witan``) exists before pushing to
it (``ensure_ecr_task``), rather than depending on the Pulumi stack
(``applications/witan``) having already created it -- this removes the
bootstrap ordering dependency between the two.

The witan stack reaches the omnigraph-server data tier (deployed by the
separate ``pulumi-omnigraph`` pipeline / ``ol-application-omnigraph`` stack)
via a StackReference; its deploy fails loudly if omnigraph has not been
deployed yet. The two ship independently — ToolHive only runs this MCP tier
and is an implementation detail, not part of the pipeline's identity.

Fly command to bootstrap this pipeline (normally set by the
``pulumi-infrastructure-meta`` meta pipeline):
    python pipeline.py
    fly -t pr-inf sp -p pulumi-witan -c definition.json
"""

import sys

from ol_concourse.lib.containers import container_build_task, ensure_ecr_task
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import (
    ECR_REGION,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)
from ol_concourse.pipelines.jobs import pulumi_jobs_chain

ENVIRONMENTS = ("CI", "QA", "Production")
IMAGE_NAME = "witan"
DOCKERFILE = "docker/witan.Dockerfile"

PULUMI_PROJECT_PATH = PULUMI_CODE_PATH.joinpath("applications/witan/")


def build_witan_pipeline() -> PipelineFragment:
    """Build the image-build + Pulumi-deploy pipeline for the witan MCP tier."""
    # Source for the image build: the agent-kit repo root (the Dockerfile
    # builds from root to reach the whole uv workspace + witan MCP servers).
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
        name=Identifier("ol-infrastructure-pulumi-witan"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_PROJECT_PATH),
        ],
    )

    # A single registry-image resource: the build job pushes one tarball here,
    # and that same image is promoted unchanged through every deploy stage
    # below (via the ``passed`` chain pulumi_jobs_chain builds from
    # ``dependencies``), rather than being pushed separately per env.
    image_resource = registry_image(
        name=Identifier(f"{IMAGE_NAME}-image"),
        image_repository=IMAGE_NAME,
        ecr_region=ECR_REGION,
    )

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
            ensure_ecr_task(IMAGE_NAME),
            PutStep(
                put=image_resource.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"{agent_kit_code.name}/.git/short_ref",
                },
            ),
        ],
    )

    # A single shared GetStep: pulumi_jobs_chain rewrites its trigger/passed
    # per stage -- CI triggers on a fresh build, QA and Production instead
    # gate (``passed``) on the *previous* stage having deployed that same
    # image, so the identical artifact promotes through every stage. The
    # image is pinned by digest (WITAN_DOCKER_SHA, read by __main__.py via
    # format_docker_image_ref) rather than a mutable ``:latest`` tag, so a
    # promoted image actually changes each stage's Deployment pod spec.
    deploy_fragment = pulumi_jobs_chain(
        refresh_stack=True,
        pulumi_code=pulumi_code,
        stack_names=list(ENVIRONMENTS),
        project_name="ol-application-witan",
        project_source_path=PULUMI_PROJECT_PATH,
        dependencies=[
            GetStep(
                get=image_resource.name,
                trigger=True,
                passed=[build_job.name],
            )
        ],
        env_vars_from_files={"WITAN_DOCKER_SHA": f"{image_resource.name}/digest"},
    )

    # combine_fragments deduplicates resources/resource-types by name (the
    # field validators only fire on assignment, not on ``.append``), so route
    # the final assembly through it rather than mutating a fragment in place.
    build_fragment = PipelineFragment(
        resources=[agent_kit_code, pulumi_code, image_resource],
        jobs=[build_job],
    )
    return PipelineFragment.combine_fragments(build_fragment, deploy_fragment)


if __name__ == "__main__":
    pipeline = build_witan_pipeline().to_pipeline()

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(("\n", "fly -t pr-inf sp -p pulumi-witan -c definition.json"))
