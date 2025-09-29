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


# Used to make a parameterized pipeline which builds
# and publishes the image from GH to ECR
courses = [
    CourseImageInfo(
        course_name="course_deep_learning_foundations_and_applications",
        repo_uri="git@github.mit.edu:ol-notebooks/course_deep_learning_foundations_and_applications.git",
        image_name="deep_learning_foundations_and_applications",
    ),
    CourseImageInfo(
        course_name="supervised_learning_fundamentals",
        repo_uri="git@github.mit.edu:ol-notebooks/course_supervised_learning_fundamentals.git",
        image_name="supervised_learning_fundamentals",
    ),
    CourseImageInfo(
        course_name="introduction_to_data_analytics_and_machine_learning",
        repo_uri="git@github.mit.edu:ol-notebooks/course_introduction_to_data_analytics_and_machine_learning.git",
        image_name="introduction_to_data_analytics_and_machine_learning",
    ),
    CourseImageInfo(
        course_name="clustering_and_descriptive_ai",
        repo_uri="git@github.mit.edu:ol-notebooks/course_clustering_and_descriptive_ai.git",
        image_name="clustering_and_descriptive_ai",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.intro",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.INTRO-2025_C604.git",
        image_name="uai_source-uai.intro",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.0",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.0-3T2025.git",
        image_name="uai_source-uai.0",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.0a",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.0A-3T2025.git",
        image_name="uai_source-uai.0a",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.1",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.1-2T2025.git",
        image_name="uai_source-uai.1",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.2",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.2-2T2025.git",
        image_name="uai_source-uai.2",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.3",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.3-3T2025.git",
        image_name="uai_source-uai.3",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.4",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.4-2025_C604.git",
        image_name="uai_source-uai.4",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.5",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.5-2025_C604.git",
        image_name="uai_source-uai.5",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.6",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.6-3T2025.git",
        image_name="uai_source-uai.6",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.7",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.7-3T2025.git",
        image_name="uai_source-uai.7",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.8",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.8-3T2025.git",
        image_name="uai_source-uai.8",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.9",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.9-3T2025.git",
        image_name="uai_source-uai.9",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.11",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.11-3T2025.git",
        image_name="uai_source-uai.11",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.12",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.12-3T2025.git",
        image_name="uai_source-uai.12",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.13",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.13-3T2025.git",
        image_name="uai_source-uai.13",
    ),
]

course_repository = ssh_git_repo(
    name=Identifier("course_name"),
    uri="((course_repo))",
    branch="main",
    private_key="((github.ol_notebooks_private_ssh_key))",
)

# This infers the ECR url from the AWS account,
# the region and the repository name
course_image = Resource(
    name=Identifier("course_image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "ol-course-notebooks",
        "tag": "((image_name))",
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
