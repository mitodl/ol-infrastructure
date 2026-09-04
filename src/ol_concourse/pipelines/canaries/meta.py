# pyright: reportCallIssue=false
"""Meta pipeline managing the fleet of Playwright canary pipelines.

Emits one ``create-<name>-pipeline`` job per entry in :data:`canary_names`, each of
which renders and sets ``canary-<name>``, plus a job that re-sets this pipeline
itself. Modeled on
``src/ol_concourse/pipelines/infrastructure/simple_pulumi/meta.py``.

:data:`canary_names` is the source of truth for what is actually deployed.
``pipeline.py``'s ``pipeline_params`` is a superset of it, so a canary can be
prepared and reviewed before it starts running against a live property.

Deployed to the production Concourse instance only -- there is deliberately no
``--env`` switch here; see ``README.md`` ("Which Concourse instance") for the
reasoning and for what would have to change to drive an internal-only target.
"""

import sys

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

CANARY_DEFINITIONS = Identifier("canary-pipeline-definitions")
CANARY_REPO_PATH = "src/ol_concourse/pipelines/canaries"

_OL_INFRA_IMAGE_SOURCE = {
    "repository": dockerhub_ecr_image_uri("mitodl/ol-infrastructure"),
    "tag": "latest",
    "aws_region": ECR_REGION,
}


def _render_pipeline_task(task_name: Identifier, script_args: list[str]) -> TaskStep:
    """Run a canary rendering script and hand ``definition.json`` to the next step.

    ``PYTHONPATH`` points at the *checked-out* ``src``, which is the point of the
    git resource: the fleet must be rendered from the commit that triggered the
    job, not from the copy baked into the image. ``ol_concourse`` is a namespace
    package in both places, so they merge -- ``ol_concourse.pipelines`` resolves
    from the checkout while ``ol_concourse.lib`` still comes from the image's
    ``ol-concourse`` install.

    :param task_name: Concourse task name.
    :param script_args: ``python`` arguments, script path first.
    :returns: The task step.
    """
    return TaskStep(
        task=task_name,
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=AnonymousResource(
                type="registry-image",
                source=_OL_INFRA_IMAGE_SOURCE,
            ),
            inputs=[Input(name=CANARY_DEFINITIONS)],
            outputs=[Output(name=Identifier("pipeline"))],
            params={"PYTHONPATH": f"../{CANARY_DEFINITIONS}/src"},
            run=Command(
                path="python",
                # definition.json is written to the CWD, so the CWD is the output
                # the SetPipelineStep reads from.
                dir="pipeline",
                user="root",
                args=script_args,
            ),
        ),
    )


def meta_job(canary_name: str) -> Job:
    """Build the job that renders and sets one property's canary pipeline.

    :param canary_name: Key into ``pipeline.py``'s ``pipeline_params``.
    :returns: A job that sets ``canary-<canary_name>``.
    """
    return Job(
        name=Identifier(f"create-{canary_name}-pipeline"),
        plan=[
            GetStep(get=CANARY_DEFINITIONS, trigger=True),
            _render_pipeline_task(
                Identifier(f"generate-{canary_name}-pipeline-file"),
                [
                    f"../{CANARY_DEFINITIONS}/{CANARY_REPO_PATH}/pipeline.py",
                    canary_name,
                ],
            ),
            SetPipelineStep(
                set_pipeline=Identifier(f"canary-{canary_name}"),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline(canary_names: list[str]) -> Pipeline:
    """Build the self-managing canary meta pipeline.

    :param canary_names: Canaries to deploy. The source of truth for the fleet.
    :returns: The pipeline to set as ``canary-meta``.
    """
    pipeline_definitions = git_repo(
        name=CANARY_DEFINITIONS,
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        # Narrower than simple_pulumi's watch list on purpose: nothing under this
        # directory imports jobs.py, secrets_map.py or versions_map.py, so a change
        # to those cannot alter a rendered canary and must not re-render the fleet.
        paths=[
            f"{CANARY_REPO_PATH}/",
            "src/ol_concourse/pipelines/constants.py",
            "pyproject.toml",
        ],
    )

    jobs = [meta_job(canary_name) for canary_name in canary_names]
    jobs.append(
        Job(
            name=Identifier("set-canary-meta-pipeline"),
            plan=[
                GetStep(get=CANARY_DEFINITIONS, trigger=True),
                _render_pipeline_task(
                    Identifier("generate-canary-meta-pipeline-file"),
                    [f"../{CANARY_DEFINITIONS}/{CANARY_REPO_PATH}/meta.py"],
                ),
                SetPipelineStep(set_pipeline="self", file="pipeline/definition.json"),
            ],
        )
    )

    return Pipeline(resources=[pipeline_definitions], jobs=jobs)


# The deployed fleet. Adding a name here is what puts a canary live, and is the
# second of the two list edits that onboarding a property takes -- the first being
# a CanaryParams entry in pipeline.py.
canary_names = [
    "mit-learn",
]


if __name__ == "__main__":
    pipeline_json = meta_pipeline(canary_names).model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p canary-meta -c definition.json")  # noqa: T201
