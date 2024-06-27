from ol_concourse.lib.jobs.infrastructure import packer_jobs
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resource_types import hashicorp_resource
from ol_concourse.lib.resources import git_repo, github_release, hashicorp_release
from ol_concourse.pipelines.constants import PACKER_WATCHED_PATHS

hashicorp_release_resource = hashicorp_resource()
vector_release = github_release(Identifier("vector-release"), "vectordotdev", "vector")
vault_agent_release = hashicorp_release(
    name=Identifier("vault-release"), project="vault"
)
consul_agent_release = hashicorp_release(
    name=Identifier("consul-release"), project="consul"
)
consul_template_release = hashicorp_release(
    name=Identifier("consul-template-release"), project="consul-template"
)

docker_baseline_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/docker_baseline_ami/",
        "src/bridge/lib/versions.py",
        *PACKER_WATCHED_PATHS,
    ],
)

ami_dependencies = [
    GetStep(get=vector_release.name, trigger=True),
    GetStep(get=vault_agent_release.name, trigger=True),
    GetStep(get=consul_agent_release.name, trigger=True),
    GetStep(get=consul_template_release.name, trigger=True),
]

docker_baseline_ami_fragment = packer_jobs(
    dependencies=ami_dependencies,
    image_code=docker_baseline_image_code,
    packer_template_path="src/bilder/images/",
    packer_vars={"app_name": "docker_baseline_ami"},
    node_types=["server"],
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
    env_vars_from_files={
        "CONSUL_VERSION": f"{consul_agent_release.name}/version",
        "VAULT_VERSION": f"{vault_agent_release.name}/version",
        "CONSUL_TEMPLATE_VERSION": f"{consul_template_release.name}/version",
    },
)

combined_fragment = PipelineFragment(
    resource_types=[
        *docker_baseline_ami_fragment.resource_types,
        hashicorp_release_resource,
    ],
    resources=docker_baseline_ami_fragment.resources,
    jobs=docker_baseline_ami_fragment.jobs,
)


docker_baseline_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        docker_baseline_image_code,
        vector_release,
        vault_agent_release,
        consul_agent_release,
        consul_template_release,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_baseline_pipeline.model_dump_json(indent=2))
    sys.stdout.write(docker_baseline_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p packer-docker-baseline -c definition.json")  # noqa: T201
