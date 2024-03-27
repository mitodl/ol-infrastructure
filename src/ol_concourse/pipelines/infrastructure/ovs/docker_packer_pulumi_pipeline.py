import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
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
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)


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

    ovs_registry_image = registry_image(
        name=Identifier("ovs-image"),
        image_repository="mitodl/ovs-app",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    ovs_packer_code = git_repo(
        name=Identifier("ol-infrastructure-packer-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=["src/bilder/images/odl_video_service", *PACKER_WATCHED_PATHS],
    )

    ovs_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            *PULUMI_WATCHED_PATHS,
            PULUMI_CODE_PATH.joinpath("applications/odl_video_service/"),
            "src/bridge/secrets/odl_video_service/",
        ],
    )

    docker_build_job = Job(
        name="build-ovs-image",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ovs_rc_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=ovs_rc_repo.name)],
                build_parameters={
                    "CONTEXT": ovs_rc_repo.name,
                    "TARGET": "production",
                },
                build_args=[],
            ),
            PutStep(
                put=ovs_registry_image.name,
                params={
                    "image": "image/image.tar",
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
        packer_template_path=(
            "src/bilder/images/odl_video_service/odl_video_service.pkr.hcl"
        ),
        env_vars_from_files={"OVS_VERSION": f"{ovs_rc_repo.name}/.git/describe_ref"},
        job_name_suffix="ovs",
    )

    pulumi_fragment = pulumi_jobs_chain(
        ovs_pulumi_code,
        stack_names=[
            f"applications.odl_video_service.{stage}" for stage in ["QA", "Production"]
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
        resources=[
            *combined_fragments.resources,
            ovs_packer_code,
            ovs_pulumi_code,
            ovs_r_repo,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_ovs_pipeline().model_dump_json(indent=2))
    sys.stdout.write(build_ovs_pipeline().model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-packer-pulumi-ovs -c definition.json")
    )
