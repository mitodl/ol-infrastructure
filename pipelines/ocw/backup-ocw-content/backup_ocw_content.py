"""Concourse pipeline to back up OCW production content S3 buckets.

Syncs draft and live OCW content buckets to their backup counterparts every 6
hours using the mitodl/concourse-s3-sync-resource.

Deploy with:

    fly -t <target> set-pipeline \\
        -p backup-ocw-content-production \\
        -c definition.json
"""

import sys

from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    InParallelStep,
    Job,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resource_types import s3_sync
from ol_concourse.lib.resources import schedule

build_schedule = schedule(
    name=Identifier("build-schedule"),
    interval="6h",
)

draft_backup = Resource(
    name=Identifier("backup-draft-content"),
    type="s3-sync",
    check_every="never",
    source={
        "bucket": "ocw-content-backup-draft-production",
        "source_bucket": "ocw-content-draft-production",
    },
)

live_backup = Resource(
    name=Identifier("backup-live-content"),
    type="s3-sync",
    check_every="never",
    source={
        "bucket": "ocw-content-backup-live-production",
        "source_bucket": "ocw-content-live-production",
    },
)

backup_pipeline = Pipeline(
    resource_types=[s3_sync()],
    resources=[build_schedule, draft_backup, live_backup],
    jobs=[
        Job(
            name=Identifier("backup-ocw-content"),
            plan=[
                GetStep(get=build_schedule.name, trigger=True),
                InParallelStep(
                    in_parallel=[
                        PutStep(put=draft_backup.name),
                        PutStep(put=live_backup.name),
                    ]
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(backup_pipeline.model_dump_json(indent=2))
    sys.stdout.write(backup_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
