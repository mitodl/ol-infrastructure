import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image

PYTHON_VERSIONS = ("3.11", "3.12", "3.13")

ol_infrastructure_repo = git_repo(
    name=Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=["dockerfiles/ol-python-base/Dockerfile"],
)

image_resources = {
    version: registry_image(
        name=Identifier(f"ol-python-base-{version.replace('.', '')}-image"),
        image_repository="mitodl/ol-python-base",
        image_tag=version,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )
    for version in PYTHON_VERSIONS
}


def build_job(python_version: str) -> Job:
    context = f"{ol_infrastructure_repo.name}/dockerfiles/ol-python-base"
    return Job(
        name=Identifier(f"build-and-publish-{python_version}"),
        public=True,
        plan=[
            GetStep(get=ol_infrastructure_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=ol_infrastructure_repo.name)],
                build_parameters={
                    "CONTEXT": context,
                    "BUILD_ARG_PYTHON_VERSION": python_version,
                },
            ),
            PutStep(
                put=image_resources[python_version].name,
                inputs="detect",
                params={"image": "image/image.tar"},
            ),
        ],
    )


ol_python_base_pipeline = Pipeline(
    resources=[ol_infrastructure_repo, *image_resources.values()],
    jobs=[build_job(v) for v in PYTHON_VERSIONS],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(ol_python_base_pipeline.model_dump_json(indent=2))
    sys.stdout.write(ol_python_base_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "\nfly -t pr-inf set-pipeline -p ol-python-base-docker -c definition.json\n"
    )
