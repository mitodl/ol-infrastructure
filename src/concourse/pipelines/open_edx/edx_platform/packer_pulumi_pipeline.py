import sys

from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from concourse.lib.resources import git_repo
from concourse.pipelines.open_edx.edx_platform.pipeline_vars import (
    RELEASE_MAP,
    DeploymentEnvRelease,
    ol_edx_deployments,
)


def filter_deployments_by_release(
    release: str, env_deployments: list[DeploymentEnvRelease]
) -> list[DeploymentEnvRelease]:
    filtered_deployments = []
    for deployment in env_deployments:
        release_match = False
        for env_tuple in deployment.env_release_map:
            if release == env_tuple.edx_release:
                release_match = True
        if release_match:
            filtered_deployments.append(deployment)
    return filtered_deployments


def build_edx_pipeline(
    release_name: str, edx_deployments: list[DeploymentEnvRelease]
) -> Pipeline:
    edx_platform_code = git_repo(
        name=Identifier("edx-platform"),
        uri="https://github.com/openedx/edx-platform",
        branch=RELEASE_MAP[release_name],
    )

    edx_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/ol_infrastructure/applications/edxapp/",
            "pipelines/infrastructure/scripts/",
            "src/ol_infrastructure/components/",
            "src/bridge/secrets/edx/",
        ],
    )

    edx_base_image_code = git_repo(
        name=Identifier("edxapp-base-code"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bilder/images/edxapp/prebuild.py",
            "src/bilder/images/edxapp/edxapp_base.pkr.hcl",
            f"src/bilder/images/edxapp/packer_vars/{release_name}.pkrvars.hcl",  # noqa: E501
        ],
    )

    base_ami_fragment = packer_jobs(
        dependencies=[GetStep(get=edx_platform_code.name, trigger=True)],
        image_code=edx_base_image_code,
        packer_template_path="src/bilder/images/edxapp/edxapp_base.pkr.hcl",
        node_types=["web", "worker"],
        extra_packer_params={
            "only": ["amazon-ebs.edxapp"],
            "var_files": [
                f"{edx_base_image_code.name}/src/bilder/images/edxapp/packer_vars/{release_name}.pkrvars.hcl"  # noqa: E501
            ],
        },
        job_name_suffix="base",
    )

    loop_resources = []
    loop_fragments = []
    for deployment in filter_deployments_by_release(release_name, edx_deployments):
        custom_image_code = git_repo(
            name=Identifier(f"edxapp-custom-image-{deployment.deployment_name}"),
            uri="https://github.com/mitodl/ol-infrastructure",
            paths=[
                "src/bilder/components/",
                "src/bilder/images/edxapp/deploy.py",
                "src/bilder/images/edxapp/group_data/",
                "src/bilder/images/edxapp/templates/vector/",
                f"src/bilder/images/edxapp/templates/edxapp/{deployment.deployment_name}/",  # noqa: E501
                "src/bilder/images/edxapp/edxapp_custom_install.pkr.hcl",
                f"src/bilder/images/edxapp/packer_vars/{deployment.deployment_name}.pkrvars.hcl",  # noqa: E501
            ],
        )
        loop_resources.append(custom_image_code)

        custom_ami_fragment = packer_jobs(
            dependencies=[
                GetStep(
                    get=base_ami_fragment.resources[-1].name,
                    trigger=True,
                    passed=[base_ami_fragment.jobs[-1].name],
                ),
            ],
            image_code=custom_image_code,
            packer_template_path="src/bilder/images/edxapp/custom_install.pkr.hcl",  # noqa: E501
            node_types=["web", "worker"],
            extra_packer_params={
                "only": ["amazon-ebs.edxapp"],
                "var_files": [
                    f"{custom_image_code.name}/src/bilder/images/edxapp/packer_vars/{deployment.deployment_name}.pkrvars.hcl"  # noqa: E501
                ],
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(custom_ami_fragment)

        edx_pulumi_fragment = pulumi_jobs_chain(
            edx_pulumi_code,
            stack_names=[
                f"applications.edxapp.{deployment.deployment_name}.{stage.environment}"
                for stage in deployment.env_release_map
                if stage.edx_release == release_name
            ],
            project_name="ol-infrastructure-edxapp-application",
            project_source_path=PULUMI_CODE_PATH.joinpath("applications/edxapp/"),
            dependencies=[
                GetStep(
                    get=custom_ami_fragment.resources[-1].name,
                    trigger=True,
                    passed=[custom_ami_fragment.jobs[-1].name],
                ),
            ],
        )
        loop_fragments.append(edx_pulumi_fragment)

    combined_fragments = PipelineFragment(
        resource_types=base_ami_fragment.resource_types
        + [
            resource_type
            for fragment in loop_fragments
            for resource_type in fragment.resource_types
        ],
        resources=base_ami_fragment.resources
        + [resource for fragment in loop_fragments for resource in fragment.resources],
        jobs=base_ami_fragment.jobs
        + [job for fragment in loop_fragments for job in fragment.jobs],
    )

    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=combined_fragments.resources
        + [edx_platform_code, edx_pulumi_code, edx_base_image_code]
        + list(loop_resources),
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_edx_pipeline(
        release_name,
        ol_edx_deployments,
    ).json(indent=2)
    with open("definition.json", "w") as definition:
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.write(
        f"fly -t <target> set-pipeline -p packer-pulumi-edxapp-{release_name} -c definition.json"  # noqa: E501
    )
