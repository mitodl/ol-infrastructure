from calendar import timegm
from datetime import datetime

from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from concourse.lib.resources import git_repo

ocw_studio = git_repo(
    Identifier("ocw-studio"),
    uri="https://github.com/mitodl/ocw-studio",
    branch="cg/replibyte-config",
    check_every="60m",
)

alpine_resource = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="marsblockchain/curl-git-jq-wget"),
)

postgres_resource = AnonymousResource(
    type=REGISTRY_IMAGE, source=RegistryImage(repository="postgres", tag="latest")
)


def db_replication_pipeline() -> Pipeline:
    db_replication_job = Job(
        name="ocw-studio-db-replication",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ocw_studio.name, trigger=False),
            TaskStep(
                task=Identifier("install-replibyte"),
                privileged=False,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=alpine_resource,
                    inputs=[Input(name=ocw_studio.name)],
                    outputs=[Output(name=ocw_studio.name)],
                    run=Command(
                        path="/bin/sh",
                        dir="./ocw-studio/replibyte",
                        args=["./install.sh"],
                    ),
                ),
            ),
            TaskStep(
                task=Identifier("replibyte-dump-create"),
                privileged=False,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=postgres_resource,
                    inputs=[Input(name=ocw_studio.name)],
                    outputs=[Output(name="dump")],
                    params={
                        "SOURCE_DB_URI": "((ocw-studio-production-database-uri))",
                        "DESTINATION_DB_URI": "((ocw-studio-qa-database-uri))",
                    },
                    run=Command(
                        path="./ocw-studio/replibyte/replibyte",
                        args=[
                            "--config",
                            "./ocw-studio/replibyte/replibyte.config",
                            "dump",
                            "create",
                        ],
                    ),
                ),
            ),
            TaskStep(
                task=Identifier("replibyte-dump-restore"),
                privileged=False,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=postgres_resource,
                    inputs=[Input(name=ocw_studio.name), Input(name="dump")],
                    params={
                        "SOURCE_DB_URI": "((ocw-studio-production-database-uri))",
                        "DESTINATION_DB_URI": "((ocw-studio-qa-database-uri))",
                    },
                    run=Command(
                        path="./ocw-studio/replibyte/replibyte",
                        args=[
                            "--config",
                            "./ocw-studio/replibyte/replibyte.config",
                            "dump",
                            "restore",
                            "remote",
                            "-v",
                            "latest",
                        ],
                    ),
                ),
            ),
        ],
    )
    return Pipeline(resources=[ocw_studio], jobs=[db_replication_job])


if __name__ == "__main__":
    import sys  # noqa: WPS433

    with open("definition.json", "w") as definition:
        definition.write(db_replication_pipeline().json(indent=2))
    sys.stdout.write(db_replication_pipeline().json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t local set-pipeline -p ocw-studio-db-replication -c definition.json"
    )
