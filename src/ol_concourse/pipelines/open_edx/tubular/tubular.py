import textwrap

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AcrossVar,
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule


def tubular_pipeline() -> Pipeline:
    tubular_config_repo = git_repo(
        name=Identifier("tubular-pipeline-config"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=["src/ol_concourse/pipelines/open_edx/tubular/"],
    )
    tubular_build_schedule = schedule(Identifier("build-schedule"), interval="168h")
    tubular_retirees = Output(name=Identifier("tubular-retirees"))
    tubular_config_path = f"{tubular_config_repo.name}/src/ol_concourse/pipelines/open_edx/tubular/openedx-config.yml"  # noqa: E501
    tubular_job_object = Job(
        name=Identifier("deploy-tubular-world"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=[
            GetStep(get=tubular_config_repo.name, trigger=True),
            GetStep(get=tubular_build_schedule.name, trigger=True),
            TaskStep(
                task=Identifier("tubular-generate-retirees-task"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="mitodl/openedx-tubular"),
                    ),
                    inputs=[Input(name=Identifier("tubular-pipeline-config"))],
                    outputs=[tubular_retirees],
                    params={
                        "TUBULAR_OAUTH_CLIENT_ID": "((tubular_oauth_client.id))",
                        "TUBULAR_OAUTH_CLIENT_SECRET": (
                            "((tubular_oauth_client.secret))"
                        ),
                        "TUBULAR_LMS_HOST": "((tubular_oauth_client.host))",
                    },
                    run=Command(
                        path="/app/scripts/get_learners_to_retire.py",
                        args=[
                            "--config_file",
                            tubular_config_path,
                            "--output_dir",
                            f"{tubular_retirees.name}/processing",
                            "--cool_off_days",
                            "0",
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
                            textwrap.dedent(
                                """\
                            import json
                            from pathlib import Path
                            learner_dir = Path("tubular-retirees/processing")
                            rfiles = learner_dir.glob("learner*")
                            retirees = []
                            for rfile in rfiles:
                                retiree = rfile.read_text().strip('\\n').split("=")[-1]
                                retirees.append(retiree)
                            with open("retirees_dir/vars.json","w") as vj:
                                vj.write(json.dumps(retirees))
                            """
                            ),
                        ],
                    ),
                    outputs=[Output(name=Identifier("retirees_dir"))],
                ),
            ),
            LoadVarStep(
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
                    inputs=[
                        Input(name=tubular_retirees.name),
                        Input(name=tubular_config_repo.name),
                    ],
                    params={
                        "TUBULAR_OAUTH_CLIENT_ID": "((tubular_oauth_client.id))",
                        "TUBULAR_OAUTH_CLIENT_SECRET": (
                            "((tubular_oauth_client.secret))"
                        ),
                        "TUBULAR_LMS_HOST": "((tubular_oauth_client.host))",
                    },
                    run=Command(
                        path="/app/scripts/retire_one_learner.py",
                        args=[
                            "--config_file",
                            tubular_config_path,
                            "--username",
                            "((.:tubular_retiree))",
                        ],
                    ),
                ),
            ),
        ],
    )
    return Pipeline(
        resources=[tubular_config_repo, tubular_build_schedule],
        jobs=[tubular_job_object],
    )


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(tubular_pipeline().model_dump_json(indent=2))
    sys.stdout.write(tubular_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p misc-cloud-tubular -c definition.json")  # noqa: T201
