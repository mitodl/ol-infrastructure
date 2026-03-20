import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import git_repo

ad_opt_repository = git_repo(
    name=Identifier("ad-opt-resource"),
    uri="https://github.com/josephine-situ/ad_opt",
    branch="main",
    check_every="24h",
)

ad_opt_release_image = Resource(
    name=Identifier("ad-opt-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/ad-opt",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

build_task = container_build_task(
    inputs=[Input(name=ad_opt_repository.name)],
    build_parameters={"CONTEXT": ad_opt_repository.name},
)

docker_pipeline = Pipeline(
    resources=[ad_opt_repository, ad_opt_release_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=ad_opt_repository.name, trigger=True),
                build_task,
                PutStep(
                    put=ad_opt_release_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{ad_opt_repository.name}/.git/describe_ref"
                        ),
                    },
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p docker-google-ads-opt-image -c definition.json"  # noqa: E501
    )
