from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    DisplayConfig,
    Identifier,
    Job,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)

from ol_concourse.pipelines.description_svg import (
    DescriptionStyle,
    write_description_svg,
)

# Raw-GitHub-hosted SVG rendering of DESCRIPTION, committed as description.svg
# alongside this file, used as the pipeline's dashboard background image
# (Concourse has no native description field; see AGENTS.md / description_svg.py).
DESCRIPTION = (
    "Prototype pipeline for validating per-pipeline description text via "
    "display.background_image. Not part of any real deployment."
)
_DESCRIPTION_SVG_RAW_URL = (
    "https://raw.githubusercontent.com/mitodl/ol-infrastructure/"
    "worktree-concourse-pipeline-descriptions/"
    "src/ol_concourse/pipelines/examples/description.svg"
)


def hello_pipeline() -> Pipeline:
    hello_job_object = Job(
        name=Identifier("deploy-hello-world"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=[
            TaskStep(
                task=Identifier("hello-task"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="busybox"),
                    ),
                    run=Command(path="echo", args=["Hello, World!"]),
                ),
            ),
        ],
    )
    return Pipeline(
        jobs=[hello_job_object],
        display=DisplayConfig(background_image=_DESCRIPTION_SVG_RAW_URL),
    )


if __name__ == "__main__":
    import sys
    from pathlib import Path

    write_description_svg(
        DESCRIPTION,
        str(Path(__file__).parent / "description.svg"),
        # vertical_position=0.5 (dead center) is the crop-safe default; nudged
        # here to dodge this pipeline's own job graph on typical viewports,
        # trading a little of that safety margin away (see description_svg.py).
        DescriptionStyle(panel_opacity=0.45, vertical_position=0.65),
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(hello_pipeline().model_dump_json(indent=2))
    sys.stdout.write(hello_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p misc-cloud-hello -c definition.json")  # noqa: T201
