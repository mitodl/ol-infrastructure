"""Concourse pipeline for the witan multi-tenant MCP service.

Two container images back the service, and BOTH build contexts live in the
``mitodl/agent-kit`` repository (NOT ol-infrastructure):

* ``omnigraph-server`` — the stateless data tier (the shared Layer-1
  memory/task/workflow graph + per-repo code graphs), built from
  ``docker/omnigraph-server.Dockerfile``.
* ``witan`` — the MCP tier, built from ``docker/witan.Dockerfile``.

Both Dockerfiles build from the agent-kit repository ROOT so they can reach the
whole uv workspace (``pyproject.toml`` / ``uv.lock`` / ``packages/**``) plus the
witan MCP servers and ``schema.pg`` that gets baked into the omnigraph-server
image. This is the crux that separates this pipeline from every existing
precedent: the image build context is a foreign repo, so we watch a dedicated
``agent-kit`` git resource for it while the Pulumi deploy runs off the
ol-infrastructure checkout.

Flow (mirrors ``kubewatch``'s build-then-deploy shape):

    agent-kit push -> build omnigraph-server image -> push to omnigraph-server-<env> ECR
                   -> build witan image           -> push to witan-<env> ECR
        -> pulumi deploy ``ol-application-toolhive-witan`` (CI -> QA -> Production),
           each stage gated (``passed``) on BOTH freshly built images.

The single ``toolhive_witan`` Pulumi project deploys both tiers; the data tier
must exist before the MCP tier (the MCP tier's ``WITAN_MEMORY_URI`` points at
the omnigraph-server ClusterIP), and that ordering is enforced *inside* the
Pulumi program by resource dependencies, so a single deploy chain gated on both
images is sufficient here — no split project / StackReference needed.

The ECR repositories (``omnigraph-server-<env>`` / ``witan-<env>``) are
provisioned by the Pulumi stack itself (``applications/toolhive_witan``); this
pipeline only builds images and pushes to them, exactly like
``kubewatch_webhook_handler``'s "ECR repo in Pulumi, image built elsewhere"
split.

Fly command to bootstrap this pipeline (normally set by the
``pulumi-infrastructure-meta`` meta pipeline):
    python pipeline.py
    fly -t pr-inf sp -p pulumi-toolhive-witan -c definition.json
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

# Deploy stages, in order. Each has its own per-env ECR repository, matching the
# ``{image}-{env_suffix.lower()}`` names the toolhive_witan Pulumi stack creates.
ENVIRONMENTS = ("CI", "QA", "Production")

# (image name, path to its Dockerfile within the agent-kit checkout). The image
# name is also the ECR repository stem the Pulumi stack provisions per env.
WITAN_IMAGES: dict[str, str] = {
    "omnigraph-server": "docker/omnigraph-server.Dockerfile",
    "witan": "docker/witan.Dockerfile",
}

PULUMI_PROJECT_PATH = PULUMI_CODE_PATH.joinpath("applications/toolhive_witan/")


def build_toolhive_witan_pipeline() -> PipelineFragment:
    """Build the image-build + Pulumi-deploy pipeline for the witan service."""
    # Source for BOTH image builds: the agent-kit repo root. Watch the paths
    # that feed either Dockerfile (the whole uv workspace + the two witan MCP
    # servers + the docker/ context).
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
        name=Identifier("ol-infrastructure-pulumi-toolhive-witan"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            str(PULUMI_PROJECT_PATH),
        ],
    )

    # One registry-image resource per (image, env): the build job pushes the
    # single freshly built tarball to each env's ECR repo, and each deploy stage
    # gates on its own env resources.
    image_resources: dict[tuple[str, str], Resource] = {}
    for image_name in WITAN_IMAGES:
        for env in ENVIRONMENTS:
            image_resources[(image_name, env)] = registry_image(
                name=Identifier(f"{image_name}-{env.lower()}-image"),
                image_repository=f"{image_name}-{env.lower()}",
                ecr_region=ECR_REGION,
            )

    # One build job per image (built once from agent-kit main), pushed to all
    # three per-env ECR repos.
    build_jobs: dict[str, Job] = {}
    for image_name, dockerfile in WITAN_IMAGES.items():
        build_jobs[image_name] = Job(
            name=Identifier(f"build-{image_name}-image"),
            build_log_retention={"builds": 10},
            plan=[
                GetStep(get=agent_kit_code.name, trigger=True),
                container_build_task(
                    inputs=[Input(name=agent_kit_code.name)],
                    build_parameters={
                        # Both Dockerfiles build from the agent-kit repo ROOT so
                        # they can reach the whole uv workspace + schema.pg.
                        "CONTEXT": agent_kit_code.name,
                        "DOCKERFILE": f"{agent_kit_code.name}/{dockerfile}",
                    },
                ),
                *[
                    PutStep(
                        put=image_resources[(image_name, env)].name,
                        params={
                            "image": "image/image.tar",
                            "additional_tags": f"{agent_kit_code.name}/.git/short_ref",
                        },
                    )
                    for env in ENVIRONMENTS
                ],
            ],
        )

    # Each deploy stage triggers off — and is gated (``passed``) on — BOTH images
    # for that env, so a stage only redeploys images this pipeline itself built.
    custom_dependencies: dict[int, list[GetStep]] = {}
    for idx, env in enumerate(ENVIRONMENTS):
        custom_dependencies[idx] = [
            GetStep(
                get=image_resources[(image_name, env)].name,
                trigger=True,
                passed=[build_jobs[image_name].name],
            )
            for image_name in WITAN_IMAGES
        ]

    deploy_fragment = pulumi_jobs_chain(
        refresh_stack=True,
        pulumi_code=pulumi_code,
        stack_names=list(ENVIRONMENTS),
        project_name="ol-application-toolhive-witan",
        project_source_path=PULUMI_PROJECT_PATH,
        custom_dependencies=custom_dependencies,
    )

    # The image-build jobs run ahead of the deploy chain; combine_fragments
    # deduplicates resources/resource-types by name (the field validators only
    # fire on assignment, not on ``.append``), so route the final assembly
    # through it rather than mutating a fragment in place.
    build_fragment = PipelineFragment(
        resources=[agent_kit_code, pulumi_code, *image_resources.values()],
        jobs=list(build_jobs.values()),
    )
    return PipelineFragment.combine_fragments(build_fragment, deploy_fragment)


if __name__ == "__main__":
    pipeline = build_toolhive_witan_pipeline().to_pipeline()

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p pulumi-toolhive-witan -c definition.json")
    )
