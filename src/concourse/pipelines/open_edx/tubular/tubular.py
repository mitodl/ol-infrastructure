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
    AcrossVar,
)
from concourse.lib.resources import git_repo


def tubular_pipeline() -> Pipeline:
    tubular_config_repo = git_repo(
        name=Identifier("tubular-pipeline-config"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="cpatti_openedx_tubular",
        paths=["src/concourse/pipelines/open_edx/tubular/"],
    )
    tubular_retirees = Output(name=Identifier("tubular-retirees"))
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
                    params={
                        "TUBULAR_OAUTH_CLIENT_ID": "((tubular_oauth_client.id))",
                        "TUBULAR_OAUTH_CLIENT_SECRET":\
                                "((tubular_oauth_client.secret))",
                        "TUBULAR_LMS_HOST": "((tubular_oauth_client.host))",
                    },
                    run=Command(
                        path="/app/scripts/get_learners_to_retire.py",
                        args=[
                            "--config_file",
                            f"{tubular_config_repo.name}/src/concourse/pipelines/open_edx/tubular/openedx-config.yml",
                            "--output_dir",
                            tubular_retirees.name,
                            "--cool_off_days",
                            "5",
                        ],
                    ),
                ),
            ),
            TaskStep(
                task=Identifier("tubular-process-retired-users-task"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="mitodl/openedx-tubular"),
                    ),
                    inputs=[Input(name=tubular_retirees.name)],
                    # inline bash script to generate retirees yaml
                    run=Command(
                        path="python",
                        args=[
                            "-c",
                            """
                            import json
                            from pathlib import Path
                            learner_dir = Path("((tubular_retirees))")
                            rfiles = learner_dir.glob("learner*")
                            retirees = [ rfile.name for rfile in rfiles ]
                            with open("vars.json","w") as vj:
                                vj.write(json.dumps(retirees))'
                            """,
                        ],
                    ),
                    outputs=[Output(name=Identifier("retirees_dir"))],
                ),
            ),
            LoadVarStep(
                #                inputs=["retirees_dir"],
                load_var="tubular_retirees",
                file="retirees_dir/vars.json",
                reveal=True,
            ),
            TaskStep(
                task=Identifier("tubular-retire-users-task"),
                across=[
                    AcrossVar(
                        var="tubular_retiree",
                        values="((.:tubular_retirees))",
                    ),
                ],
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="mitodl/openedx-tubular"),
                    ),
                    inputs=[Input(name=tubular_retirees.name)],
                    run=Command(
                        path="/app/scripts/retire_one_learner.py",
                        args=[
                            "--config_file",
                            f"{tubular_config_repo.name}/src/concourse/pipelines/open_edx/tubular/openedx_config.yml",
                            "--username",
                            "((.:tubular_retiree))",
                        ],
                    ),
                ),
            ),
        ],
    )
    tubular_pipeline = Pipeline(jobs=[tubular_job_object])
    return tubular_pipeline


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(tubular_pipeline().json(indent=2))
    sys.stdout.write(tubular_pipeline().json(indent=2))
    print()
    print("fly -t pr-inf sp -p misc-cloud-tubular -c definition.json")
