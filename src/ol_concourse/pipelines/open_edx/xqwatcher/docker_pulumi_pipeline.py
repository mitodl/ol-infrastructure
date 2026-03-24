import sys

from bridge.settings.openedx.accessors import filter_deployments_by_application
from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_xqwatcher_pipeline(release_name: str):
    xqwatcher_repo = git_repo(
        name=Identifier("xqueue-watcher-code"),
        uri="https://github.com/mitodl/xqueue-watcher",
        branch="master",
    )

    xqwatcher_registry_image = registry_image(
        name=Identifier("xqueue-watcher-container"),
        image_repository="mitodl/xqueue-watcher",
        image_tag="latest",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    xqwatcher_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-deploy"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/xqwatcher/"),
            "src/bridge/settings/openedx/",
        ],
    )

    image_build_job = Job(
        name=Identifier("build-xqueue-watcher-image"),
        plan=[
            GetStep(get=xqwatcher_repo.name, trigger=True),
            container_build_task(
                inputs=[
                    Input(name=xqwatcher_repo.name),
                ],
                build_parameters={
                    "CONTEXT": xqwatcher_repo.name,
                    "DOCKERFILE": f"{xqwatcher_repo.name}/Dockerfile",
                },
            ),
            PutStep(
                put=xqwatcher_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{xqwatcher_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[xqwatcher_repo, xqwatcher_registry_image],
        jobs=[image_build_job],
    )

    loop_fragments = []
    for deployment in filter_deployments_by_application(release_name, "xqwatcher"):
        pulumi_fragment = pulumi_jobs_chain(
            xqwatcher_pulumi_code,
            stack_names=[
                f"applications.xqwatcher.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
            ],
            project_name="ol-infrastructure-xqwatcher-server",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/xqwatcher/"),
            dependencies=[
                GetStep(
                    get=container_fragment.resources[-1].name,
                    trigger=True,
                    passed=[container_fragment.jobs[-1].name],
                ),
            ],
            env_vars_from_files={
                "XQWATCHER_DOCKER_DIGEST": f"{xqwatcher_registry_image.name}/digest"
            },
        )
        loop_fragments.append(pulumi_fragment)

    combined_fragments = PipelineFragment.combine_fragments(
        container_fragment,
        *loop_fragments,
    )

    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
            xqwatcher_pulumi_code,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    from bridge.settings.openedx.types import OpenEdxSupportedRelease

    if len(sys.argv) < 2:  # noqa: PLR2004
        releases = [r.name for r in OpenEdxSupportedRelease]
        sys.stderr.write(
            f"Usage: {sys.argv[0]} <release_name>\n"
            f"Available releases: {', '.join(releases)}\n"
        )
        sys.exit(1)
    release_name = sys.argv[1]
    pipeline_json = build_xqwatcher_pipeline(
        release_name,
    ).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.writelines(
        (
            "\n",
            (
                "fly -t <target> set-pipeline -p"
                f" docker-pulumi-xqwatcher-{release_name} -c definition.json"
            ),
        )
    )
