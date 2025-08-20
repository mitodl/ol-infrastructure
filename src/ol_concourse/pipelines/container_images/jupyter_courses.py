import dataclasses
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
from ol_concourse.lib.resources import ssh_git_repo


@dataclasses.dataclass
class CourseImageInfo:
    course_name: str
    repo_uri: str
    image_name: str


# Used to make a parameterized pipeline which builds and publishes the image from GH to ECR
courses = [
    CourseImageInfo(
        course_name="course_deep_learning_foundations_and_applications",
        repo_uri="git@github.mit.edu:ol-notebooks-qa/course_deep_learning_foundations_and_applications.git",
        image_name="deep_learning_foundations_and_applications",
    ),
    CourseImageInfo(
        course_name="supervised_learning_fundamentals",
        repo_uri="git@github.mit.edu:ol-notebooks-qa/course_supervised_learning_fundamentals.git",
        image_name="supervised_learning_fundamentals",
    ),
    CourseImageInfo(
        course_name="introduction_to_data_analytics_and_machine_learning",
        repo_uri="git@github.mit.edu:ol-notebooks-qa/course_introduction_to_data_analytics_and_machine_learning.git",
        image_name="introduction_to_data_analytics_and_machine_learning",
    ),
    CourseImageInfo(
        course_name="clustering_and_descriptive_ai",
        repo_uri="git@github.mit.edu:ol-notebooks-qa/course_clustering_and_descriptive_ai.git",
        image_name="clustering_and_descriptive_ai",
    ),
]

course_repository = ssh_git_repo(
    name=Identifier("course_name"),
    uri="((course_repo))",
    branch="main",
    private_key="((ol_notebooks_private_ssh_key))",
)

# Shouldn't need the AWS account ID as it'll infer it from the host's creds. Docs are a bit squiggy,
# but the hope is that setting aws_region should allow it to construct the ECR hostname from the account ID and region.
# TODO: This is untested, as we don't yet have an ECR repo set up for testing.
course_image = Resource(
    name=Identifier("course_image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "ol-course-notebooks/((image_name))",
        "tag": "latest",
        "aws_region": "us-east-1",
    },
)

build_task = container_build_task(
    inputs=[Input(name=course_repository.name)],
    build_parameters={"CONTEXT": course_repository.name},
)

docker_pipeline = Pipeline(
    resources=[course_repository, course_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=course_repository.name, trigger=True),
                build_task,
                PutStep(
                    put=course_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{course_repository.name}/.git/describe_ref",  # noqa: E501
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
    for course in courses:
        sys.stdout.write(
            f"fly -t <prod_target> set-pipeline -p jupyter_notebook_docker_image_build -c definition.json --var course_repo={course.repo_uri} --instance-var image_name={course.image_name}\n"  # noqa: E501
        )
