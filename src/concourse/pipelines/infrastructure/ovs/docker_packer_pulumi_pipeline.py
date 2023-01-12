#  noqa: WPS232
import sys
import textwrap

from concourse.lib.constants import (
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
    REGISTRY_IMAGE,
)
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import (  # noqa: WPS235
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
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from concourse.lib.resources import git_repo, registry_image


def build_ovs_pipeline() -> Pipeline:
    ovs_rc_branch = "release-candidate"
    ovs_r_branch = "release"

    ovs_rc_repo = git_repo(
        Identifier("odl-video-service-rc"),
        uri="https://github.com/mitodl/odl-video-service",
        branch=ovs_rc_branch,
    )

    # This is only used to trigger deployment to production
    # no artifacts from this resource should actaully be utilized
    ovs_r_repo = git_repo(
        Identifier("odl-video-service-release"),
        uri="https://github.com/mitodl/odl-video-service",
        branch=ovs_r_branch,
    )

    ovs_registry_image = registry_image(  # noqa: S106
        name=Identifier("ovs-image"),
        image_repository="mitodl/ovs-app",
        username="((dockerhub.username))",
        password="((dockerhub.password))",
    )

    ovs_packer_code = git_repo(
        name=Identifier("ol-infrastructure-packer-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/bilder/images/odl_video_service",
        ],
    )

    ovs_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=PULUMI_WATCHED_PATHS
        + [
            PULUMI_CODE_PATH.joinpath("applications/odl_video_service/"),
        ],
    )

    dcind_resource = AnonymousResource(
        type=REGISTRY_IMAGE,
        source=RegistryImage(repository="mitodl/concourse-dcind", tag="latest"),
    )

    # TODO MD 20230110
    # May be able to convert this to something that uses 'concourse.lib.tasks.container_build_task'
    docker_build_job = Job(
        name="build-ovs-image",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ovs_rc_repo.name, trigger=True),
            TaskStep(
                task=Identifier("build-ovs-container"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=dcind_resource,
                    inputs=[Input(name=ovs_rc_repo.name)],
                    outputs=[Output(name=ovs_rc_repo.name)],
                    run=Command(
                        path="/bin/entrypoint.sh",
                        args=[
                            "bash",
                            "-ceux",
                            textwrap.dedent(
                                f"""mount -t cgroup -o none,name=systemd cgroup /sys/fs/cgroup/systemd
                                cd {ovs_rc_repo.name}
                                chmod 777 .
                                docker build . -f Dockerfile --target=production -t ovs-app:latest
                                FINAL_IMG_ID="$(docker images | grep "ovs-app" |  tr -s ' ' | cut -d ' ' -f 3)"
                                docker save "${{FINAL_IMG_ID}}" -o ovs-app.tar"""
                            ),  # noqa: WPS355
                        ],
                    ),
                ),
            ),
            PutStep(
                put=ovs_registry_image.name,
                params={
                    "image": f"{ovs_rc_repo.name}/ovs-app.tar",
                    "additional_tags": f"./{ovs_rc_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[ovs_rc_repo, ovs_registry_image],
        jobs=[docker_build_job],
    )

    ami_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=ovs_registry_image.name,
                trigger=True,
                passed=[docker_build_job.name],
            ),
            GetStep(get=ovs_rc_repo.name, trigger=False),
        ],
        image_code=ovs_packer_code,
        packer_template_path="src/bilder/images/odl_video_service/odl_video_service.pkr.hcl",
        env_vars_from_files={"OVS_VERSION": f"{ovs_rc_repo.name}/.git/describe_ref"},
        job_name_suffix="ovs",
    )

    pulumi_fragment = pulumi_jobs_chain(
        ovs_pulumi_code,
        stack_names=[
            f"applications.odl_video_service.{stage}"
            for stage in ["QA", "Production"]  # noqa: WPS335
        ],
        project_name="ol-infrastrcuture-ovs-server",
        project_source_path=PULUMI_CODE_PATH.joinpath(
            "applications/odl_video_service/"
        ),
        dependencies=[
            GetStep(
                get=ami_fragment.resources[-1].name,
                trigger=True,
                passed=[ami_fragment.jobs[-1].name],
            ),
        ],
        custom_dependencies={
            0: [
                GetStep(
                    get=ovs_rc_repo.name,
                    trigger=False,
                    passed=[ami_fragment.jobs[-1].name],
                )
            ],
            # Need to ensure the resource below is included when returning the pipeline
            # since it is never referenced before now.
            1: [
                GetStep(
                    get=ovs_r_repo.name,
                    trigger=True,
                )
            ],
        },
    )

    combined_fragments = PipelineFragment.combine_fragments(
        container_fragment, ami_fragment, pulumi_fragment
    )
    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=combined_fragments.resources
        + [ovs_packer_code, ovs_pulumi_code, ovs_r_repo],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:
        definition.write(build_ovs_pipeline().json(indent=2))
    sys.stdout.write(build_ovs_pipeline().json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-ovs -c definition.json")
    )
