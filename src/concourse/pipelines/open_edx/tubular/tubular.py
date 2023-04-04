from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    TaskStep,
    Identifier,
    Job,
    LoadVarStep,
    Output,
    Input,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
    AcrossVar
)



def tubular_pipeline() -> Pipeline:
    tubular_retirees=Output(
                name=Identifier("tubular-retirees")
            )
    tubular_job_object = Job(
        name=Identifier("deploy-tubular-world"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=[
            TaskStep(
                task=Identifier("tubular-generate-retirees-task"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="mitodl/openedx-tubular"),
                    ),
                    outputs=[tubular_retirees],
                    run=Command(
                        path="/app/scripts/get_learners_to_retire.py",
                        # I have no idea if /etc is the right place for the configs to live,
                        # Or what the config should look like.
                        args=[
                            "--config_file", "/etc/openedx_config.yml",
                            "--output_dir", tubular_retirees.name,
                            "--cool_off_days", "5"
                            ]
                    ),
                ),
            ),
            LoadVarStep(
                load_var=Identifier("tubular_retirees"),
                file=Identifier(tubular_retirees.name),
                reveal=True,
            ),
            TaskStep(
                task=Identifier("tubular-retire-users-task"),
                across=AcrossVar(
                    var=tubular_retirees,
                    values=tubular_retirees,
                    fail_fast=True,
                ),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="mitodl/openedx-tubular"),
                    ),
                    inputs=[
                        Input(
                            name=tubular_retirees.name
                            )
                        ],
                    run=Command(
                        path="/app/scripts/retire_one_learner.py",
                        # I have no idea if /etc is the right place for the configs to live,
                        # Or what the config should look like.
                        args=[
                            "--config_file", "/etc/openedx_config.yml",
                            ]
                    ),
                ),
            ),
        ],
    )
    tubular_pipeline = Pipeline(
            jobs=[tubular_job_object]
            )
    return tubular_pipeline


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(tubular_pipeline().json(indent=2))
    sys.stdout.write(tubular_pipeline().json(indent=2))
    print()
    print("fly -t pr-inf sp -p misc-cloud-tubular -c definition.json")
