"""Concourse pipeline for the omnigraph-server service.

The ``omnigraph-server`` image build context lives in the ``mitodl/agent-kit``
repo (NOT ol-infrastructure): ``docker/omnigraph-server.Dockerfile`` builds
from the agent-kit repo ROOT so it can reach the whole uv workspace plus the
``schema.pg`` baked into the image. This is the crux that separates this
pipeline from every existing ol-infrastructure precedent — the build context
is a foreign repo — so it watches a dedicated ``agent-kit`` git resource for
the image and runs the Pulumi deploy off the ol-infrastructure checkout.

Flow (mirrors ``kubewatch``'s build-then-deploy shape):

    agent-kit push -> build omnigraph-server image -> push to omnigraph-server ECR
        -> pulumi deploy ``ol-application-omnigraph`` CI, gated on that image
           -> pulumi deploy QA, gated (``passed``) on CI having deployed that
              same image -> Production, gated on QA. One image is built once
              and promoted unchanged through every stage, matching every
              other build+deploy pipeline in this repo (e.g. ``k8s_apps``).

The build job ensures the ECR repository (``omnigraph-server``) exists before
pushing to it (``ensure_ecr_task``), rather than depending on the Pulumi
stack (``applications/omnigraph``) having already created it -- this removes
the bootstrap ordering dependency between the two.

witan (``pulumi-witan``) is a separate pipeline deploying a separate stack that
reaches this service via a StackReference; the two ship independently.

Fly command to bootstrap this pipeline (normally set by the
``pulumi-infrastructure-meta`` meta pipeline):
    python pipeline.py
    fly -t pr-inf sp -p pulumi-omnigraph -c definition.json
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
from ol_concourse.pipelines.ecr import configure_ecr_repository_task
from ol_concourse.pipelines.jobs import pulumi_jobs_chain
from ol_concourse.pipelines.secrets_map import project_secrets_paths
from ol_concourse.pipelines.versions_map import project_version_paths

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
            *project_version_paths("applications/omnigraph/"),
            *project_secrets_paths("applications/omnigraph/"),
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
            configure_ecr_repository_task(IMAGE_NAME),
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
    # image is pinned by digest (OMNIGRAPH_DOCKER_SHA, read by data_tier.py
    # via format_docker_image_ref) rather than a mutable ``:latest`` tag, so
    # a promoted image actually changes each stage's Deployment pod spec.
    deploy_fragment = pulumi_jobs_chain(
        refresh_stack=True,
        pulumi_code=pulumi_code,
        stack_names=list(ENVIRONMENTS),
        project_name="ol-application-omnigraph",
        project_source_path=PULUMI_PROJECT_PATH,
        dependencies=[
            GetStep(
                get=image_resource.name,
                trigger=True,
                passed=[build_job.name],
            )
        ],
        env_vars_from_files={"OMNIGRAPH_DOCKER_SHA": f"{image_resource.name}/digest"},
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
    pipeline = build_omnigraph_pipeline().to_pipeline()

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p pulumi-omnigraph -c definition.json")
    )
