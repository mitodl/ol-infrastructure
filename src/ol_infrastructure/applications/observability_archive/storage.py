"""Object storage and IRSA roles for the archive backends.

One bucket and one IAM role per backend. The Helm charts own the Kubernetes
ServiceAccounts, so the role ARN is handed to them as an annotation rather than
created here.
"""

from dataclasses import dataclass
from typing import Any, cast

from pulumi import Output, ResourceOptions
from pulumi_aws import iam, s3

from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo

# The S3 lifecycle rule is a backstop behind the compactor-enforced retention,
# not the mechanism, so it trails it by a week (D12).
LIFECYCLE_BACKSTOP_DAYS = 7

# Mimir refuses to start when blocks and ruler state share a bucket and prefix
# (D6), so the ruler gets its own prefix and the lifecycle rule only ever expires
# blocks. Loki's compactor manages every prefix in its bucket, so its rule is
# bucket-wide.
BACKENDS: dict[str, dict[str, str]] = {
    "loki": {"service_account": "loki", "expire_prefix": ""},
    "mimir": {"service_account": "mimir", "expire_prefix": "blocks/"},
}
MIMIR_BLOCKS_STORAGE_PREFIX = "blocks"
MIMIR_RULER_STORAGE_PREFIX = "ruler"


@dataclass
class ArchiveBackendStorage:
    """What a backend's Helm release needs to know about its storage."""

    bucket: OLBucket
    bucket_name: str
    irsa_role: OLEKSTrustRole
    service_account: str


def _bucket_policy_document(bucket_name: str) -> dict[str, Any]:
    return {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": f"arn:aws:s3:::{bucket_name}",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            },
        ],
    }


def build(  # noqa: PLR0913
    *,
    stack_info: StackInfo,
    aws_config: AWSBase,
    retention_days: int,
    namespace: str,
    account_id: str,
    cluster_name: Output[str],
    cluster_identities: Output[Any],
) -> dict[str, ArchiveBackendStorage]:
    """Create the bucket and IRSA role for each backend, keyed by backend name."""
    env = stack_info.env_suffix
    storage: dict[str, ArchiveBackendStorage] = {}
    for backend, spec in BACKENDS.items():
        bucket_name = f"ol-telemetry-archive-{backend}-{env}"
        bucket = OLBucket(
            f"telemetry-archive-{backend}-{env}",
            config=S3BucketConfig(
                bucket_name=bucket_name,
                versioning_enabled=False,
                # SSE-S3 rather than the component's aws:kms default: a KMS key
                # would add a Decrypt call per chunk read and a key policy per
                # role for no gain on data that is not confidential at rest
                # beyond what the bucket policy already enforces.
                server_side_encryption_enabled=True,
                sse_algorithm="AES256",
                lifecycle_rules=[
                    s3.BucketLifecycleConfigurationRuleArgs(
                        id="expire-past-retention",
                        status="Enabled",
                        filter=s3.BucketLifecycleConfigurationRuleFilterArgs(
                            prefix=spec["expire_prefix"]
                        ),
                        expiration=s3.BucketLifecycleConfigurationRuleExpirationArgs(
                            days=retention_days + LIFECYCLE_BACKSTOP_DAYS
                        ),
                    )
                ],
                tags=aws_config.tags,
            ),
        )
        policy = iam.Policy(
            f"telemetry-archive-{backend}-s3-policy-{env}",
            name=f"telemetry-archive-{backend}-{env}",
            path="/ol-infrastructure/telemetry-archive/",
            description=f"Read/write access to the {backend} archive bucket",
            policy=cast(
                "str",
                lint_iam_policy(_bucket_policy_document(bucket_name), stringify=True),
            ),
            opts=ResourceOptions(parent=bucket),
        )
        role = OLEKSTrustRole(
            f"telemetry-archive-{backend}-irsa-{env}",
            role_config=OLEKSTrustRoleConfig(
                account_id=account_id,
                cluster_name=cluster_name,
                cluster_identities=cluster_identities,
                description=f"IRSA role for the {backend} telemetry archive",
                policy_operator="StringEquals",
                role_name=f"telemetry-archive-{backend}",
                service_account_name=spec["service_account"],
                service_account_namespace=namespace,
                tags=aws_config.tags,
            ),
        )
        iam.RolePolicyAttachment(
            f"telemetry-archive-{backend}-s3-policy-attach-{env}",
            policy_arn=policy.arn,
            role=role.role.name,
            opts=ResourceOptions(parent=role),
        )
        storage[backend] = ArchiveBackendStorage(
            bucket=bucket,
            bucket_name=bucket_name,
            irsa_role=role,
            service_account=spec["service_account"],
        )
    return storage
