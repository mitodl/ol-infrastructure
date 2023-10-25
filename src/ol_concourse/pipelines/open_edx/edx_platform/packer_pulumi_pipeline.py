import sys

from bridge.settings.openedx.accessors import filter_deployments_by_release
from bridge.settings.openedx.types import DeploymentEnvRelease, OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment

from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_edx_pipeline(
    release_name: str,
    edx_deployments: list[DeploymentEnvRelease],  # noqa: ARG001
) -> Pipeline:
    edx_platform_code = git_repo(
        name=Identifier("edx-platform"),
        uri="https://github.com/openedx/edx-platform",
        branch=(
            "2u/release"
            if release_name == "master"
            else OpenEdxSupportedRelease[release_name].branch
        ),
    )

    edx_pulumi_code = git_repo(
        name=Identifier("ol-infrastructure-pulumi"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            *PULUMI_WATCHED_PATHS,
            "src/ol_infrastructure/applications/edxapp/",
            "src/bridge/secrets/edxapp/",
            "src/bridge/settings/openedx/",
        ],
    )

    edx_base_image_code = git_repo(
        name=Identifier("edxapp-base-code"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bridge/settings/openedx/",
            "src/bilder/images/edxapp/prebuild.py",
            "src/bilder/images/edxapp/edxapp_base.pkr.hcl",
            f"src/bilder/images/edxapp/packer_vars/{release_name}.pkrvars.hcl",
            "src/bilder/images/edxapp/files/edxapp_web_playbook.yml",
            "src/bilder/images/edxapp/files/edxapp_worker_playbook.yml",
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
                f"{edx_base_image_code.name}/src/bilder/images/edxapp/packer_vars/{release_name}.pkrvars.hcl"
            ],
        },
        job_name_suffix="base",
    )

    loop_resources = []
    loop_fragments = []
    for deployment in filter_deployments_by_release(release_name):
        custom_image_code = git_repo(
            name=Identifier(f"edxapp-custom-image-{deployment.deployment_name}"),
            uri="https://github.com/mitodl/ol-infrastructure",
            paths=[
                "src/bridge/settings/openedx/",
                "src/bilder/components/",
                "src/bilder/images/edxapp/deploy.py",
                "src/bilder/images/edxapp/group_data/",
                "src/bilder/images/edxapp/templates/vector/",
                f"src/bilder/images/edxapp/templates/edxapp/{deployment.deployment_name}/",
                "src/bilder/images/edxapp/custom_install.pkr.hcl",
                f"src/bilder/images/edxapp/packer_vars/{deployment.deployment_name}.pkrvars.hcl",
                f"src/bilder/images/edxapp/packer_vars/{release_name}.pkrvars.hcl",
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
            packer_template_path="src/bilder/images/edxapp/custom_install.pkr.hcl",
            node_types=["web", "worker"],
            extra_packer_params={
                "only": ["amazon-ebs.edxapp"],
                "var_files": [
                    f"{custom_image_code.name}/src/bilder/images/edxapp/packer_vars/{release_name}.pkrvars.hcl",
                    f"{custom_image_code.name}/src/bilder/images/edxapp/packer_vars/{deployment.deployment_name}.pkrvars.hcl",
                ],
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(custom_ami_fragment)

        edx_pulumi_fragment = pulumi_jobs_chain(
            edx_pulumi_code,
            stack_names=[
                f"applications.edxapp.{deployment.deployment_name}.{stage}"
                for stage in deployment.envs_by_release(release_name)
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

    combined_fragments = PipelineFragment.combine_fragments(
        base_ami_fragment,
        *loop_fragments,
    )

    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
            edx_platform_code,
            edx_pulumi_code,
            edx_base_image_code,
            *list(loop_resources),
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    release_name = sys.argv[1]
    pipeline_json = build_edx_pipeline(
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
                f" packer-pulumi-edxapp-{release_name} -c definition.json"
            ),
        )
    )
