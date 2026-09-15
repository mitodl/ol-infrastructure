"""RustFS S3-compatible object storage for the local-dev infra stack.

Deployed only when an app that needs object storage is enabled (see
OBJECT_STORE_APPS) — it is the one shared service heavy enough that running
it for developers who don't need it is worth avoiding.

RustFS rather than MinIO: MinIO's community server is effectively frozen and
RustFS is the drop-in S3-compatible replacement we are standardising on. The
S3 surface ocw-studio exercises (canned public-read ACLs on upload, bucket
policies, versioning, presigned URLs, anonymous path-style GETs) was verified
against rustfs 1.0.0-rc.6 before this module was written.

One deliberate difference from the compose setup it replaces: the buckets are
made world-readable with a bucket policy, not `mc anonymous set public`.
RustFS does not implement S3 ACLs at all — it accepts the canned ACL header
boto3 sends and ignores it — so a policy is the only grant that actually takes
effect. The draft/live/test site routes read from the object store with no
credentials, so without that grant every published page would 403.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

# Apps whose manifests expect an in-cluster S3 endpoint. Adding an app here is
# what causes RustFS to be deployed; see create_object_store's callers.
# odl-video-service is a candidate — its app-env.yaml currently carries stub
# AWS values and a "no real S3 in local dev" note — but it has not been
# switched over, so it is deliberately not listed yet.
OBJECT_STORE_APPS = ("ocw-studio",)

# Pinned rather than :latest — RustFS is pre-1.0 and its release candidates
# are not API-stable with each other.
RUSTFS_IMAGE = "rustfs/rustfs:1.0.0-rc.6"

# Local-dev credentials. These are not secret in any meaningful sense: the
# whole cluster is local and every app manifest carries the same literal pair.
# They are duplicated in local-dev/apps/ocw-studio/secrets.yaml.
ACCESS_KEY = "localdevaccess"
SECRET_KEY = "localdevsecret123"  # noqa: S105  # pragma: allowlist secret

# Buckets created at bootstrap, mirroring ocw-studio's .env.example names so a
# developer's existing local settings keep working. Every one is granted
# anonymous read; ocw-studio's published output is public content.
OCW_STUDIO_BUCKETS = (
    "ol-ocw-studio-app-local",
    "ocw-content-draft-local",
    "ocw-content-live-local",
    "ocw-content-test",
    "ocw-content-offline-draft-local",
    "ocw-content-offline-live-local",
    "ocw-content-offline-test",
    "ol-eng-artifacts-local",
    # Source bucket for the storage import/sync pipeline.
    "ol-ocw-studio-app-import-local",
)

# Concourse resource versioning reads this bucket's object versions, so it
# needs versioning turned on the way the real artifacts bucket does.
VERSIONED_BUCKETS = ("ol-eng-artifacts-local",)

# Idempotent: re-running it on an existing store re-asserts the policies and
# leaves the objects alone, so the Job can be replayed after a config change.
_BOOTSTRAP_SCRIPT = """#!/bin/sh
set -eu

s3api() {
    aws --endpoint-url "${S3_ENDPOINT}" s3api "$@"
}

public_read_policy() {
    cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"AnonymousRead","Effect":"Allow","Principal":{"AWS":["*"]},"Action":["s3:GetObject"],"Resource":["arn:aws:s3:::$1/*"]}]}
JSON
}

echo "==> waiting for the object store to answer"
until aws --endpoint-url "${S3_ENDPOINT}" s3api list-buckets >/dev/null 2>&1; do
    sleep 2
done

for bucket in ${PUBLIC_BUCKETS}; do
    if s3api head-bucket --bucket "${bucket}" >/dev/null 2>&1; then
        echo "==> bucket exists: ${bucket}"
    else
        echo "==> creating bucket: ${bucket}"
        s3api create-bucket --bucket "${bucket}" >/dev/null
    fi
    s3api put-bucket-policy --bucket "${bucket}" \
        --policy "$(public_read_policy "${bucket}")"
    echo "    anonymous read granted"
done

for bucket in ${VERSIONED_BUCKETS:-}; do
    echo "==> enabling versioning: ${bucket}"
    s3api put-bucket-versioning --bucket "${bucket}" \
        --versioning-configuration Status=Enabled
done

echo "==> object storage bootstrap complete"
"""


@dataclass
class ObjectStoreResources:
    stateful_set: k8s.apps.v1.StatefulSet
    service: k8s.core.v1.Service
    credentials: k8s.core.v1.Secret
    bootstrap_job: k8s.batch.v1.Job


def create_object_store(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
    apisix_release: k8s.helm.v3.Release,
    tls_secret: k8s.core.v1.Secret,
    s3_hostname: str,
    buckets: tuple[str, ...] = OCW_STUDIO_BUCKETS,
) -> ObjectStoreResources:
    """Deploy RustFS plus a Job that creates and opens up the buckets.

    The S3 API is reachable in-cluster at
    rustfs.local-infra.svc.cluster.local:9000 and from the browser at
    https://{s3_hostname} — apps need both, because pod-side boto3 calls and
    the URLs handed to a browser cannot use the same address.
    """
    credentials = k8s.core.v1.Secret(
        "rustfs-credentials",
        metadata={"name": "rustfs-credentials", "namespace": "local-infra"},
        string_data={
            "RUSTFS_ACCESS_KEY": ACCESS_KEY,
            "RUSTFS_SECRET_KEY": SECRET_KEY,
        },
        opts=_k8s(parent=local_infra_ns),
    )

    stateful_set = k8s.apps.v1.StatefulSet(
        "rustfs",
        metadata={"name": "rustfs", "namespace": "local-infra"},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "rustfs"}},
            "serviceName": "rustfs",
            "template": {
                "metadata": {"labels": {"app": "rustfs"}},
                "spec": {
                    "containers": [
                        {
                            "name": "rustfs",
                            "image": RUSTFS_IMAGE,
                            # The image's entrypoint takes the data directory
                            # as a positional argument.
                            "args": ["/data"],
                            "env": [
                                {"name": "RUSTFS_ADDRESS", "value": ":9000"},
                                # The embedded console is a second listener we
                                # have no route for; the S3 API is the whole
                                # reason this is here.
                                {
                                    "name": "RUSTFS_CONSOLE_ENABLE",
                                    "value": "false",
                                },
                                {
                                    "name": "RUSTFS_OBS_LOGGER_LEVEL",
                                    "value": "warn",
                                },
                            ],
                            "envFrom": [
                                {"secretRef": {"name": "rustfs-credentials"}},
                            ],
                            "ports": [{"containerPort": 9000, "name": "s3"}],
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 9000},
                                "initialDelaySeconds": 3,
                                "periodSeconds": 5,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": 9000},
                                "initialDelaySeconds": 15,
                                "periodSeconds": 30,
                                "failureThreshold": 6,
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"memory": "1Gi"},
                            },
                        }
                    ],
                },
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "20Gi"}},
                    },
                }
            ],
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[credentials]),
    )

    service = k8s.core.v1.Service(
        "rustfs-svc",
        metadata={"name": "rustfs", "namespace": "local-infra"},
        spec={
            "selector": {"app": "rustfs"},
            "ports": [{"name": "s3", "port": 9000, "targetPort": 9000}],
        },
        opts=_k8s(parent=stateful_set),
    )

    bootstrap_job = k8s.batch.v1.Job(
        "rustfs-bootstrap-buckets",
        metadata={
            "name": "rustfs-bootstrap-buckets",
            "namespace": "local-infra",
        },
        spec={
            # The Job body is idempotent but its spec is immutable, so bumping
            # the bucket list means Pulumi replaces the Job rather than
            # patching it.
            "backoffLimit": 6,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": {"app": "rustfs-bootstrap"}},
                "spec": {
                    "restartPolicy": "OnFailure",
                    "containers": [
                        {
                            "name": "bootstrap",
                            "image": "amazon/aws-cli:2.36.44",
                            "command": ["/bin/sh", "/scripts/bootstrap.sh"],
                            "env": [
                                {
                                    "name": "S3_ENDPOINT",
                                    "value": (
                                        "http://rustfs.local-infra"
                                        ".svc.cluster.local:9000"
                                    ),
                                },
                                {
                                    "name": "AWS_ACCESS_KEY_ID",
                                    "value": ACCESS_KEY,
                                },
                                {
                                    "name": "AWS_SECRET_ACCESS_KEY",
                                    "value": SECRET_KEY,
                                },
                                {
                                    "name": "AWS_DEFAULT_REGION",
                                    "value": "us-east-1",
                                },
                                {
                                    "name": "PUBLIC_BUCKETS",
                                    "value": " ".join(buckets),
                                },
                                {
                                    "name": "VERSIONED_BUCKETS",
                                    "value": " ".join(VERSIONED_BUCKETS),
                                },
                            ],
                            "volumeMounts": [
                                {"name": "scripts", "mountPath": "/scripts"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "scripts",
                            "configMap": {"name": "rustfs-bootstrap-script"},
                        }
                    ],
                },
            },
        },
        opts=_k8s(parent=stateful_set, depends_on=[service]),
    )

    k8s.core.v1.ConfigMap(
        "rustfs-bootstrap-script",
        metadata={
            "name": "rustfs-bootstrap-script",
            "namespace": "local-infra",
        },
        data={"bootstrap.sh": _BOOTSTRAP_SCRIPT},
        opts=_k8s(parent=local_infra_ns),
    )

    # Browser-facing S3 endpoint. Apps hand the browser URLs under this host
    # (django-storages' AWS_S3_CUSTOM_DOMAIN); pod-side boto3 keeps using the
    # in-cluster Service address instead.
    k8s.apiextensions.CustomResource(
        "rustfs-apisix-route",
        api_version="apisix.apache.org/v2",
        kind="ApisixRoute",
        metadata={"name": "rustfs-route", "namespace": "local-infra"},
        spec={
            "ingressClassName": "apache-apisix",
            "http": [
                {
                    "name": "rustfs",
                    "match": {"hosts": [s3_hostname], "paths": ["/*"]},
                    "backends": [{"serviceName": "rustfs", "servicePort": 9000}],
                    "plugins": [
                        {
                            "name": "cors",
                            "enable": True,
                            "config": {
                                "allow_origins": "**",
                                "allow_methods": "GET,HEAD,OPTIONS",
                                "allow_headers": "*",
                            },
                        }
                    ],
                }
            ],
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, service]),
    )

    k8s.apiextensions.CustomResource(
        "rustfs-apisix-tls",
        api_version="apisix.apache.org/v2",
        kind="ApisixTls",
        metadata={"name": "rustfs-tls", "namespace": "local-infra"},
        spec={
            "ingressClassName": "apache-apisix",
            "hosts": [s3_hostname],
            "secret": {"name": "local-dev-tls", "namespace": "local-infra"},
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[apisix_release, tls_secret]),
    )

    return ObjectStoreResources(
        stateful_set=stateful_set,
        service=service,
        credentials=credentials,
        bootstrap_job=bootstrap_job,
    )
