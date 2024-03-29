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
    ovs_m_branch = "master"
    ovs_rc_branch = "release-candidate"
    ovs_r_branch = "release"

    ovs_m_repo = git_repo(
        Identifier("odl-video-service-master"),
        uri="https://github.com/mitodl/odl-video-service",
        branch=ovs_m_branch,
    )

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
        ranch="main",
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

    m_docker_build_job = Job(
        name="build-ovs-image-from-master",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ovs_m_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=ovs_m_repo.name)],
                build_parameters={
                    "CONTEXT": ovs_m_repo.name,
                    "TARGET": "production",
                },
                build_args=[],
            ),
            PutStep(
                put=ovs_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{ovs_m_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    m_container_fragment = PipelineFragment(
        resources=[ovs_m_repo, ovs_registry_image],
        jobs=[m_docker_build_job],
    )

    m_ami_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=ovs_registry_image.name,
                trigger=True,
                passed=[m_docker_build_job.name],
            ),
            GetStep(get=ovs_m_repo.name, trigger=False),
        ],
        image_code=ovs_packer_code,
        packer_template_path=(
            "src/bilder/images/odl_video_service/odl_video_service.pkr.hcl"
        ),
        packer_vars={"branch": ovs_m_branch},
        env_vars_from_files={"OVS_VERSION": f"{ovs_m_repo.name}/.git/describe_ref"},
        job_name_suffix="ovs-master",
    )

    m_pulumi_fragment = pulumi_jobs_chain(
        ovs_pulumi_code,
        stack_names=[f"applications.odl_video_service.{stage}" for stage in ["CI"]],
        project_name="ol-infrastrcuture-ovs-server",
        project_source_path=PULUMI_CODE_PATH.joinpath(
            "applications/odl_video_service/"
        ),
        dependencies=[
            GetStep(
                get=m_ami_fragment.resources[-1].name,
                trigger=True,
                passed=[m_ami_fragment.jobs[-1].name],
            ),
        ],
        enable_github_issue_resource=False,
    )

    m_combined_fragments = PipelineFragment.combine_fragments(
        m_container_fragment, m_ami_fragment, m_pulumi_fragment
    )

    rc_docker_build_job = Job(
        name="build-ovs-image-from-rc",
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

    rc_container_fragment = PipelineFragment(
        resources=[ovs_rc_repo, ovs_registry_image],
        jobs=[rc_docker_build_job],
    )

    rc_ami_fragment = packer_jobs(
        dependencies=[
            GetStep(
                get=ovs_registry_image.name,
                trigger=True,
                passed=[rc_docker_build_job.name],
            ),
            GetStep(get=ovs_rc_repo.name, trigger=False),
        ],
        image_code=ovs_packer_code,
        packer_template_path=(
            "src/bilder/images/odl_video_service/odl_video_service.pkr.hcl"
        ),
        packer_vars={"branch": ovs_rc_branch},
        env_vars_from_files={"OVS_VERSION": f"{ovs_rc_repo.name}/.git/describe_ref"},
        job_name_suffix="ovs-rc",
    )

    rc_pulumi_fragment = pulumi_jobs_chain(
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
                get=rc_ami_fragment.resources[-1].name,
                trigger=True,
                passed=[rc_ami_fragment.jobs[-1].name],
            ),
        ],
        custom_dependencies={
            0: [
                GetStep(
                    get=ovs_rc_repo.name,
                    trigger=False,
                    passed=[rc_ami_fragment.jobs[-1].name],
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

    rc_combined_fragments = PipelineFragment.combine_fragments(
        rc_container_fragment, rc_ami_fragment, rc_pulumi_fragment
    )

    combined_fragments = PipelineFragment.combine_fragments(
        m_combined_fragments, rc_combined_fragments
    )
    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
            ovs_packer_code,
            ovs_pulumi_code,
            ovs_r_repo,
            # ovs_m_repo,
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
