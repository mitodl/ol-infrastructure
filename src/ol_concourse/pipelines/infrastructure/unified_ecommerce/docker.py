import sys

from ol_concourse.lib.containers import container_build_task
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


def build_ecommerce_pipeline() -> Pipeline:
    ecommerce_m_branch = "main"

    ecommerce_repo = git_repo(
        Identifier("unified-ecommerce-master"),
        uri="https://github.com/mitodl/unified-ecommerce",
        branch=ecommerce_m_branch,
    )

    # This is only used to trigger deployment to production
    # no artifacts from this resource should actaully be utilized

    ecommerce_registry_image = registry_image(
        name=Identifier("unified-ecommerce-image"),
        image_repository="mitodl/unified-ecommerce-app-main",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    m_docker_build_job = Job(
        name="build-ecommerce-image-from-main",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ecommerce_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=ecommerce_repo.name)],
                build_parameters={
                    "CONTEXT": ecommerce_repo.name,
                },
                build_args=[],
            ),
            PutStep(
                put=ecommerce_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{ecommerce_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    container_fragment = PipelineFragment(
        resources=[ecommerce_repo, ecommerce_registry_image],
        jobs=[m_docker_build_job],
    )

    return container_fragment.to_pipeline()


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(build_ecommerce_pipeline().model_dump_json(indent=2))
    sys.stdout.write(build_ecommerce_pipeline().model_dump_json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-unified-ecommerce -c definition.json")
    )
