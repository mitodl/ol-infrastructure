# ruff: noqa: ERA001 PERF401
import textwrap
from pathlib import Path

from bridge.secrets.sops import read_yaml_secrets

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Job,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule

qa_dyno_map = {
    "ocw-studio-ci": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=0:Standard-1x",
            "release=0:Standard-1X",
            # "web=1:Standard-1X",
            "worker=1:Standard-1X",
        ],
    },
    "ocw-studio-rc": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=1:Standard-2x",
            "release=0:Standard-1X",
            # "web=1:Standard-2X",
            "worker=1:Standard-2X",
        ],
    },
    "micromasters-rc": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=0:Standard-1x",
            # "web=1:Standard-1X",
            "worker=1:Standard-1X",
        ],
    },
    "bootcamp-ecommerce-rc": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=0:Standard-1x",
            # "web=1:Standard-1X",
            "worker=1:Standard-1X",
        ],
    },
    "xpro-rc": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=0:Standard-1x",
            "release=0:Standard-1X",
            # "web=1:Standard-1X",
            "worker=1:Standard-1X",
        ],
    },
    "mitxonline-rc": {
        "owner": "odl-devops",
        "dyno_list": [
            "extra_worker=0:Standard-1X",
            # "web=1:Standard-2X",
            "worker=1:Performance-M",
        ],
    },
    "odl-open-discussions-rc": {
        "owner": "odl-devops",
        "dyno_list": [
            "extra_worker_performance=0:Performance-L",
            "extra_worker_2x=0:Standard-2X",
            "release=0:Standard-1X",
            # "web=1:Standard-1X",
            "worker=1:Standard-1X",
        ],
    },
    "mitopen-rc": {
        "owner": "ol-engineering-finance",
        "dyno_list": [
            "extra_worker_performance=0:Performance-L",
            "extra_worker_2x=0:Standard-2X",
            "release=0:Standard-1X",
            # "web=3:Standard-1X",
            "worker=1:Standard-2X",
        ],
    },
}

production_dyno_map = {
    "ocw-studio": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=2:Standard-2x",
            "release=0:Standard-1X",
            # "web=5:Standard-2X",
            "worker=1:Standard-2X",
        ],
    },
    "micromasters": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=2:Standard-2x",
            # "web=3:Standard-2X",
            "worker=1:Standard-2X",
        ],
    },
    "bootcamp-ecommerce-rc": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=0:Standard-1x",
            # "web=3:Standard-2X",
            "worker=1:Standard-2X",
        ],
    },
    "xpro": {
        "owner": "mitx-devops",
        "dyno_list": [
            "extra_worker=0:Standard-2x",
            "release=0:Standard-1X",
            # "web=5:Standard-2X",
            "worker=1:Performance-M",
        ],
    },
    "mitxonline-production": {
        "owner": "odl-devops",
        "dyno_list": [
            "extra_worker=0:Performance-M",
            # "web=3:Standard-2X",
            "worker=1:Performance-M",
        ],
    },
    "odl-open-discussions": {
        "owner": "odl-devops",
        "dyno_list": [
            "extra_worker_performance=1:Performance-L",
            "extra_worker_2x=0:Standard-2X",
            "release=0:Standard-1X",
            # "web=3:Standard-1X",
            "worker=1:Standard-1X",
        ],
    },
    "mitopen-production": {
        "owner": "ol-engineering-finance",
        "dyno_list": [
            "extra_worker_performance=0:Performance-L",
            "extra_worker_2x=0:Standard-2X",
            "release=0:Standard-1X",
            # "web=2:Standard-2X",
            "worker=1:Standard-2X",
        ],
    },
}

qa_build_schedule = schedule(
    Identifier("qa-build-schedule"),
    days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    start="7AM",
    stop="8AM",
)
production_build_schedule = schedule(
    Identifier("production-build-schedule"),
    days=["Monday", "Friday"],
    start="8PM",
    stop="9PM",
)
ol_infra_git_repo = git_repo(
    Identifier("ol-infra-repo"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
)

heroku_cli_resource = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="mitodl/heroku-cli", tag="latest"),
)


qa_jobs = []
for app_name, app_config in qa_dyno_map.items():
    heroku_api_key = read_yaml_secrets(
        Path().joinpath("heroku", f"secrets.{app_config['owner']}.yaml")
    )["apiKey"]

    task_list = [GetStep(get=qa_build_schedule.name, trigger=True)]

    for dyno_config in app_config["dyno_list"]:
        task_list.append(
            TaskStep(
                task=Identifier(f"reset-{dyno_config.split('=')[0]}"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=heroku_cli_resource,
                    inputs=[],
                    outputs=[],
                    run=Command(
                        path="sh",
                        args=[
                            "-ec",
                            textwrap.dedent(
                                f"""
                            export HEROKU_API_KEY={heroku_api_key}
                            heroku dyno:scale -a {app_name} {dyno_config}
                            """
                            ),
                        ],
                    ),
                ),
            )
        )
    task_list.append(
        TaskStep(
            task=Identifier("show-dyno-config"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=heroku_cli_resource,
                inputs=[],
                outputs=[],
                run=Command(
                    path="sh",
                    args=[
                        "-ec",
                        textwrap.dedent(
                            f"""
                        export HEROKU_API_KEY={heroku_api_key}
                        heroku dyno:type -a {app_name}
                        """
                        ),
                    ],
                ),
            ),
        )
    )

    qa_jobs.append(
        Job(
            name=f"reset-dynos-nonprod-{app_name}",
            build_log_retention={"days": 30},
            plan=task_list,
        )
    )

qa_combined_fragment = PipelineFragment(
    resources=[qa_build_schedule],
    jobs=qa_jobs,
)

production_jobs = []
for app_name, app_config in production_dyno_map.items():
    heroku_api_key = read_yaml_secrets(
        Path().joinpath("heroku", f"secrets.{app_config['owner']}.yaml")
    )["apiKey"]

    task_list = [GetStep(get=production_build_schedule.name, trigger=True)]

    for dyno_config in app_config["dyno_list"]:
        task_list.append(
            TaskStep(
                task=Identifier(f"reset-{dyno_config.split('=')[0]}"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=heroku_cli_resource,
                    inputs=[],
                    outputs=[],
                    run=Command(
                        path="sh",
                        args=[
                            "-ec",
                            textwrap.dedent(
                                f"""
                            export HEROKU_API_KEY={heroku_api_key}
                            heroku dyno:scale -a {app_name} {dyno_config}
                            """
                            ),
                        ],
                    ),
                ),
            )
        )
    task_list.append(
        TaskStep(
            task=Identifier("show-dyno-config"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=heroku_cli_resource,
                inputs=[],
                outputs=[],
                run=Command(
                    path="sh",
                    args=[
                        "-ec",
                        textwrap.dedent(
                            f"""
                        export HEROKU_API_KEY={heroku_api_key}
                        heroku dyno:type -a {app_name}
                        """
                        ),
                    ],
                ),
            ),
        )
    )

    production_jobs.append(
        Job(
            name=f"reset-dynos-prod-{app_name}",
            build_log_retention={"days": 30},
            plan=task_list,
        )
    )

production_combined_fragment = PipelineFragment(
    resources=[production_build_schedule],
    jobs=production_jobs,
)

fully_combined_fragment = PipelineFragment(
    resource_types=qa_combined_fragment.resource_types
    + production_combined_fragment.resource_types,
    resources=qa_combined_fragment.resources + production_combined_fragment.resources,
    jobs=qa_combined_fragment.jobs + production_combined_fragment.jobs,
)

grafana_pipeline = Pipeline(
    resource_types=fully_combined_fragment.resource_types,
    resources=fully_combined_fragment.resources,
    jobs=fully_combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(grafana_pipeline.model_dump_json(indent=2))
    sys.stdout.write(grafana_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p misc-heroku-dyno-management -c definition.json"
    )  # noqa: RUF100, T201
