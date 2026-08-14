import sys

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)
from ol_concourse.pipelines.jobs import packer_jobs, pulumi_jobs_chain
from ol_concourse.pipelines.secrets_map import project_secrets_paths
from ol_concourse.pipelines.versions_map import (
    image_version_paths,
    project_version_paths,
)

#############
# RESOURCES #
#############
# No github_release resource here on purpose. install_concourse
# (src/bilder/components/concourse/steps.py) downloads the release archive
# directly from GitHub using just the version string -- it never reads
# anything else out of a release fetch -- so CONCOURSE_VERSION is sourced
# straight from the version pin file below instead of a separate tracked
# resource. That pin is bumped by Renovate (via versions.py + the
# sync-version-pins pre-commit hook, see PR #5407) and concourse_image_code's
# watch on image_version_paths("concourse") is what triggers a rebuild --
# never upstream cutting a new release. That auto-tracking (with no tag_filter
# and no human gate) is how 8.3.0 reached production and broke every worker
# (see PR #5401).
concourse_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/concourse",
        *image_version_paths("concourse"),
        *PACKER_WATCHED_PATHS,
    ],
)

concourse_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/applications/concourse",
        *project_version_paths("applications/concourse/"),
        *project_secrets_paths("applications/concourse/"),
    ],
)

concourse_ami_fragment = packer_jobs(
    dependencies=[],
    image_code=concourse_image_code,
    packer_template_path="src/bilder/images/.",
    node_types=["web", "worker"],
    packer_vars={"app_name": "concourse"},
    env_vars_from_files={
        "CONCOURSE_VERSION": (
            f"{concourse_image_code.name}/src/bridge/lib/version_pins/CONCOURSE_VERSION"
        )
    },
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

concourse_pulumi_fragment = pulumi_jobs_chain(
    concourse_pulumi_code,
    refresh_stack=True,
    stack_names=["CI", "QA", "Production"],
    project_name="ol-infrastructure-concourse-application",
    project_source_path=PULUMI_CODE_PATH.joinpath("applications/concourse/"),
    dependencies=[
        GetStep(
            get=concourse_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[concourse_ami_fragment.jobs[-1].name],
        )
    ],
)


combined_fragment = PipelineFragment(
    resource_types=concourse_ami_fragment.resource_types
    + concourse_pulumi_fragment.resource_types,
    resources=concourse_ami_fragment.resources + concourse_pulumi_fragment.resources,
    jobs=concourse_ami_fragment.jobs + concourse_pulumi_fragment.jobs,
)


def concourse_pipeline() -> Pipeline:
    return Pipeline(
        resource_types=combined_fragment.resource_types,
        resources=[
            *combined_fragment.resources,
            concourse_image_code,
            concourse_pulumi_code,
        ],
        jobs=combined_fragment.jobs,
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(concourse_pipeline().model_dump_json(indent=2))
    sys.stdout.write(concourse_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p packer-pulumi-concourse -c definition.json"
    )  # noqa: RUF100, T201
