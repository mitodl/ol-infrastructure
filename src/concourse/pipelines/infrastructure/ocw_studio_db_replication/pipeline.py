from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
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

postgres_resource = AnonymousResource(
    type=REGISTRY_IMAGE, source=RegistryImage(repository="postgres", tag="12")
)


def db_replication_pipeline() -> Pipeline:
    db_replication_job = Job(
        name="restore-db",
        build_log_retention={"builds": 10},
        plan=[
            TaskStep(
                task=Identifier("run-pg-dump"),
                privileged=False,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=postgres_resource,
                    outputs=[Output(name="dump")],
                    params={
                        "PGHOST": "((source.host))",
                        "PGPORT": "((source.port))",
                        "PGUSER": "((source.username))",
                        "PGPASSWORD": "((source.password))",
                    },
                    run=Command(
                        path="/bin/sh",
                        args=[
                            "-c",
                            "pg_dump -Fc -d ((source.database)) > ./dump/db.dump",
                        ],
                    ),
                ),
            ),
            TaskStep(
                task=Identifier("run-pg-restore"),
                privileged=False,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=postgres_resource,
                    inputs=[Input(name="dump")],
                    params={
                        "PGHOST": "((destination.host))",
                        "PGPORT": "((destination.port))",
                        "PGUSER": "((destination.username))",
                        "PGPASSWORD": "((destination.password))",
                    },
                    run=Command(
                        path="/bin/sh",
                        args=[
                            "-c",
                            "pg_restore --clean --no-privileges --no-owner -d ((destination.database)) ./dump/db.dump",
                        ],
                    ),
                ),
            ),
        ],
    )
    return Pipeline(jobs=[db_replication_job])


if __name__ == "__main__":
    import sys  # noqa: WPS433

    with open("definition.json", "w") as definition:
        definition.write(db_replication_pipeline().json(indent=2))
    sys.stdout.write(db_replication_pipeline().json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t local set-pipeline -p ocw-studio-db-replication -c definition.json\n"
    )
