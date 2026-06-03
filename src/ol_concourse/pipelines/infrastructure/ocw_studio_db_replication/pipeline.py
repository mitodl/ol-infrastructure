from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    TaskConfig,
    TaskStep,
)

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

postgres_resource = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={
        "repository": dockerhub_ecr_image_uri("postgres"),
        "tag": "12",
        "aws_region": ECR_REGION,
    },
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
                            "pg_dump -v -Fc -d ((source.database)) > ./dump/db.dump",
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
                            (
                                "pg_restore -v --clean --no-privileges --no-owner -d"
                                " ((destination.database)) ./dump/db.dump"
                            ),
                        ],
                    ),
                ),
            ),
        ],
    )
    return Pipeline(jobs=[db_replication_job])


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(db_replication_pipeline().model_dump_json(indent=2))
    sys.stdout.write(db_replication_pipeline().model_dump_json(indent=2))
    sys.stdout.write("\n")
    sys.stdout.write(
        "fly -t qa-ocw set-pipeline -p ocw-studio-db-replication -c definition.json\n"
    )
