from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.pulumi_helper import parse_stack

# AWS Permissions Document
# The OCW site-publishing subset of iam_policies/ocw.py, for the shared
# 'generic' worker pool.
#
# ocw-studio builds its site pipelines on the 'ocw' Concourse team but sets no
# step tags, so Concourse may place them on any worker serving that team -- the
# team-scoped 'ocw' pool or the global untagged 'generic' pool. Landing on
# 'generic' failed with AccessDenied on s3:ListBucket, since that pool carries
# only base/ecr_push/operations/pulumi_state.
#
# Attaching the full 'ocw' policy would also hand every other team sharing the
# generic pool (main, infrastructure, and the per-deployment Open edX teams) the
# legacy open-learning-course-data* import buckets, so this grants only the
# buckets ocw-studio's site_pipeline.py actually reads and writes, scoped to
# this stack's environment. The artifacts bucket (ol-eng-artifacts, holding the
# webpack manifest) is deliberately absent -- 'operations' already covers it on
# every non-ocw worker.

stack_info = parse_stack()

# web_bucket and offline_bucket, for both the draft and live deployments.
publish_buckets = [
    f"ocw-content-draft-{stack_info.env_suffix}",
    f"ocw-content-live-{stack_info.env_suffix}",
    f"ocw-content-offline-draft-{stack_info.env_suffix}",
    f"ocw-content-offline-live-{stack_info.env_suffix}",
]

policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            # storage_bucket: the uploaded course resources that
            # build-online-site and build-offline-site sync down. Read-only --
            # only ocw-studio itself writes here.
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:ListBucket",
            ],
            "Resource": [
                f"arn:aws:s3:::ol-ocw-studio-app-{stack_info.env_suffix}",
                f"arn:aws:s3:::ol-ocw-studio-app-{stack_info.env_suffix}/*",
            ],
        },
        {
            # upload-online-build and upload-offline-build publish here with
            # `s3 sync --delete` and `s3 rm --recursive`, hence DeleteObject.
            # The multipart actions are what large-file syncs fall back to;
            # PutObject alone covers initiate and complete, but not an abort
            # partway through.
            "Effect": "Allow",
            "Action": [
                "s3:AbortMultipartUpload",
                "s3:DeleteObject",
                "s3:GetObject*",
                "s3:ListBucket",
                "s3:ListMultipartUploadParts",
                "s3:PutObject",
            ],
            "Resource": [
                *(f"arn:aws:s3:::{bucket}" for bucket in publish_buckets),
                *(f"arn:aws:s3:::{bucket}/*" for bucket in publish_buckets),
            ],
        },
    ],
}
