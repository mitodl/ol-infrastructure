from ol_concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Pipeline,
)
from ol_concourse.lib.resource_types import hashicorp_resource
from ol_concourse.lib.resources import git_repo, hashicorp_release
from ol_concourse.pipelines.constants import (
    PACKER_WATCHED_PATHS,
    PULUMI_CODE_PATH,
    PULUMI_WATCHED_PATHS,
)

vault_release = hashicorp_release(Identifier("vault-release"), "vault")
vault_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/vault/",
        "src/bilder/components/hashicorp/",
        *PACKER_WATCHED_PATHS,
    ],
)
vault_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[*PULUMI_WATCHED_PATHS, "src/ol_infrastructure/infrastructure/vault/"],
)
vault_pulumi_substructure_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-substructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[*PULUMI_WATCHED_PATHS, "src/ol_infrastructure/substructure/vault/"],
)

get_vault_release = GetStep(get=vault_release.name, trigger=True)

vault_ami_fragment = packer_jobs(
    dependencies=[get_vault_release],
    image_code=vault_image_code,
    packer_template_path="src/bilder/images/",
    packer_vars={"app_name": "vault"},
    node_types=["server"],
    env_vars_from_files={"VAULT_VERSION": "vault-release/version"},
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

vault_pulumi_fragment = pulumi_jobs_chain(
    vault_pulumi_code,
    project_name="ol-infrastructure-vault-server",
    stack_names=[
        f"infrastructure.vault.operations.{stage}"
        for stage in ("CI", "QA", "Production")
    ],
    project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/vault/"),
    dependencies=[
        GetStep(
            get=vault_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[vault_ami_fragment.jobs[-1].name],
        )
    ],
)

substructure_fragments = []

for substructure in [
    "pki",
    "static_mounts",
    "auth",
    "encryption_mounts",
    "secrets",
    "setup",
]:
    substructure_fragments.append(  # noqa: PERF401
        pulumi_jobs_chain(
            vault_pulumi_substructure_code,
            project_name=f"ol-infrastructure-vault-{substructure}",
            stack_names=[
                f"substructure.vault.{substructure}.operations.{stage}"
                for stage in ("CI", "QA", "Production")
            ],
            project_source_path=PULUMI_CODE_PATH.joinpath(
                f"substructure/vault/{substructure}/"
            ),
        )
    )

combined_fragment = PipelineFragment.combine_fragments(
    vault_ami_fragment, vault_pulumi_fragment, *substructure_fragments
)

vault_pipeline = Pipeline(
    resource_types=[*combined_fragment.resource_types, hashicorp_resource()],
    resources=[
        *combined_fragment.resources,
        vault_image_code,
        vault_release,
        vault_pulumi_code,
        vault_pulumi_substructure_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(vault_pipeline.model_dump_json(indent=2))
    sys.stdout.write(vault_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-pulumi-vault -c definition.json")  # noqa: T201
