import sys

from bridge.settings.openedx.accessors import filter_deployments_by_application

from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Pipeline,
)
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS


def build_xqwatcher_pipeline(release_name: str):
    xqwatcher_packer_code = git_repo(
        name=Identifier("ol-infrastructure-build"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=[
            "src/bridge/settings/openedx/",
            "src/bilder/images/xqwatcher/",
        ],
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

    loop_fragments = []
    for deployment in filter_deployments_by_application(release_name, "xqwatcher"):
        ami_fragment = packer_jobs(
            dependencies=[],
            image_code=xqwatcher_packer_code,
            packer_template_path="src/bilder/images/xqwatcher/xqwatcher.pkr.hcl",
            packer_vars={
                "deployment": deployment.deployment_name,
                "openedx_release": release_name,
            },
            job_name_suffix=deployment.deployment_name,
        )
        loop_fragments.append(ami_fragment)

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
                    get=ami_fragment.resources[-1].name,
                    trigger=True,
                    passed=[ami_fragment.jobs[-1].name],
                ),
            ],
        )
        loop_fragments.append(pulumi_fragment)

    combined_fragments = PipelineFragment.combine_fragments(*loop_fragments)

    return Pipeline(
        resource_types=combined_fragments.resource_types,
        resources=[
            *combined_fragments.resources,
            xqwatcher_pulumi_code,
            xqwatcher_packer_code,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
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
                f" packer-pulumi-xqwatcher-{release_name} -c definition.json"
            ),
        )
    )
