# ruff: noqa: E501
import sys

from bridge.settings.openedx.accessors import filter_deployments_by_release
from bridge.settings.openedx.types import DeploymentEnvRelease, OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
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
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image
from ol_concourse.pipelines.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)


def build_notes_pipeline(
    release_name: str,
    edx_deployments: list[DeploymentEnvRelease],  # noqa: ARG001
):
    openedx_release = OpenEdxSupportedRelease[release_name]
    notes_branch = openedx_release.branch
    notes_repo = git_repo(
        name=Identifier("openedx-notes-code"),
        uri="https://github.com/openedx/edx-notes-api",
        branch=notes_branch,
    )

    notes_registry_image = registry_image(
        name=Identifier("openedx-notes-container"),
        image_repository="mitodl/openedx-notes",
        image_tag=release_name,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    notes_lehrer_repo = git_repo(
        name=Identifier("notes-lehrer"),
        uri="https://github.com/mitodl/lehrer",
        branch="main",
    )

    notes_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-deploy"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/edx_notes/"),
            "src/bridge/settings/openedx/",
            "src/bridge/secrets/edx_notes/",
        ],
    )

    image_build_job = Job(
        name=Identifier("build-notes-image"),
        plan=[
            GetStep(get=notes_repo.name, trigger=True),
            GetStep(get=notes_lehrer_repo.name, trigger=True),
            TaskStep(
                task=Identifier("build-notes-image"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": "mitodl/dcind",
                            "tag": "0.20.3",
                        },
                    ),
                    inputs=[
                        Input(name=notes_repo.name),
                        Input(name=notes_lehrer_repo.name),
                    ],
                    outputs=[Output(name="artifacts")],
                    run=Command(
                        path="bash",
                        args=[
                            "-c",
                            f"""source /docker-lib.sh;
                            start_docker;
                            cd {notes_lehrer_repo.name};
                            PYTHON_VERSION={openedx_release.python_version};
                            DAGGER_LOG_LEVEL=debug dagger call build_notes --notes-config ./notes_config --python-version $PYTHON_VERSION --release-name {release_name} export --path ../artifacts/image.tar;
                            """,
                        ],
                    ),
                ),
            ),
            PutStep(
                put=notes_registry_image.name,
                params={
                    "image": "artifacts/image.tar",
                    "additional_tags": f"./{notes_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[
            notes_repo,
            notes_registry_image,
            notes_lehrer_repo,
        ],
        jobs=[image_build_job],
    )

    loop_fragments = []
    for deployment in filter_deployments_by_release(release_name):
        pulumi_fragment = pulumi_jobs_chain(
            notes_pulumi_code,
            stack_names=[
                f"applications.edxnotes.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
            ],
            project_name="ol-infrastructure-notes-server",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/edx_notes/"),
            dependencies=[
                GetStep(
                    get=container_fragment.resources[-1].name,
                    trigger=True,
                    passed=[container_fragment.jobs[-1].name],
                ),
                GetStep(
                    get=notes_registry_image.name,
                    trigger=False,
                    passed=[image_build_job.name],
                ),
            ],
            env_vars_from_files={
                "EDX_NOTES_DOCKER_DIGEST": f"{notes_registry_image.name}/digest"
            },
        )
        loop_fragments.append(pulumi_fragment)

    combined_fragments = PipelineFragment.combine_fragments(
        container_fragment,
        *loop_fragments,
    )

    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[*combined_fragments.resources, notes_pulumi_code],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_notes_pipeline(
        release_name,
        OpenLearningOpenEdxDeployment,
    ).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.writelines(
        (
            "\n",
            (
                "fly -t <target> set-pipeline -p"
                f" dagger-pulumi-notes-{release_name} -c definition.json"
            ),
        )
    )
