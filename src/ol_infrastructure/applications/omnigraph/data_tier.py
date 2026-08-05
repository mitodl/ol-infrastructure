"""The omnigraph data tier: a stateless ``omnigraph-server`` Deployment.

ADR-0009 (ol-infrastructure) decision point 2 / option 3: the data tier is a
plain Kubernetes ``Deployment`` (no PVC/StatefulSet — state lives entirely in
S3) reached by consumers (today, the ``witan`` MCPServer in the ``witan``
namespace) over the cluster network only, never exposed outside the cluster.
This mirrors the Redis-behind-vMCP precedent ``toolhive_swe`` uses for its own
stateful backend, except S3-backed so the pod itself needs no persistent
volume.

``omnigraph-server`` (an external Rust binary,
https://github.com/ModernRelay/omnigraph — not vendored in either repo) boots
from a ``--cluster <config-dir>`` pointing at a ``cluster.yaml`` (see
``docs/agent-memory.md`` "Remote Team Server" in agent-kit for the reference
CLI invocation this follows). That file is generated here as a ConfigMap
rather than hand-authored, so the S3 bucket name/region and graph list stay
in lockstep with the Pulumi-managed bucket.

A pre-deploy ``omnigraph cluster apply`` Job runs ahead of the Deployment on
every deploy, creating newly-declared graphs and applying schema updates to
existing ones from the schemas baked into the image. Nothing else reconciles a
live graph with a changed schema — a graph keeps whatever schema it was created
with — so without this step an image whose code reads a newly-added field fails
against the deployed graph. The server only serves the converged revision after
a restart, hence Job-then-Deployment ordering.

The Job and the Deployment's pod template carry the same
``ol.mit.edu/config-hash`` (cluster.yaml + image digest), which is what
keeps converge and restart in lockstep: any change the Job acts on also
restarts the server into it, including a cluster.yaml-only edit that leaves the
image untouched. That makes a config change a brief data-tier outage — this is
a single-replica ``Recreate`` Deployment — which is the deliberate trade for
never serving a config the store has already moved past.

The ``omnigraph-server`` image is built once and promoted unchanged through
CI -> QA -> Production by the ``omnigraph``/``pulumi-omnigraph`` Concourse
pipeline, which also owns the ``omnigraph-server`` ECR repository itself
(idempotent create-if-missing on every build) rather than this stack
creating it — a single repo can't be owned by three independent per-env
Pulumi stacks. This stack instead pins the Deployment's image by digest,
read from ``OMNIGRAPH_DOCKER_SHA`` (set by the build job via the
pulumi-provisioner's ``env_vars_from_files``).
"""

import hashlib
import json
import re
from typing import NamedTuple

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import yaml
from pulumi import Output, ResourceOptions

from ol_infrastructure.applications.omnigraph.maintenance import (
    OmnigraphMaintenance,
    create_maintenance,
)
from ol_infrastructure.components.applications.eks import OLEKSAuthBinding
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.vault import OLVaultK8SSecret
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo, format_docker_image_ref

OMNIGRAPH_SERVER_SERVICE_NAME = "omnigraph-server"
OMNIGRAPH_SERVER_PORT = 8080
OMNIGRAPH_SERVICE_ACCOUNT_NAME = "omnigraph-server"

# Mount path for the generated cluster.yaml ConfigMap (--cluster <config-dir>
# expects schema.pg alongside it — see CLUSTER_CONFIG_DIR below) and for the
# actor-tokens Secret (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE), matching the
# WITAN_ACTOR_TOKENS_FILE mount witan itself uses in mcp_servers.py so both
# processes read the *same* generated artifact per agent-kit ADR-0004 D3.
CLUSTER_CONFIG_DIR = "/etc/omnigraph/cluster"
ACTOR_TOKENS_MOUNT_PATH = "/etc/omnigraph/actor-tokens"  # pragma: allowlist secret
ACTOR_TOKENS_FILENAME = "tokens.json"  # pragma: allowlist secret

# Actor recorded in the commit ledger for the converge job's direct-engine
# writes. cluster.yaml declares no `policy:` block today, so omnigraph does not
# *require* an actor — this is passed for an attributable audit trail, and so
# that if a policy bundle is added later the job fails loudly (denied) rather
# than silently writing as nobody. Matches the break-glass identity in
# agent-kit ADR-0005 path (b).
CLUSTER_APPLY_ACTOR = "svc-witan-admin"

# ── Server resource envelope ─────────────────────────────────────────────────
#
# Named rather than inlined because the per-actor admission caps below are
# derived from the memory limit, and a limit raised without revisiting the caps
# would silently leave them mis-sized.
SERVER_MEMORY_LIMIT = "2Gi"

# ── Per-actor admission caps ─────────────────────────────────────────────────
#
# omnigraph-server admits writes per actor and 429s (with Retry-After) over the
# limit; Cedar authz runs first, so this is a resource guard, not an authz one.
# Both env vars are confirmed present in the 0.8.1 server binary.
#
# WHY THE DEFAULTS ARE WRONG FOR THIS DEPLOYMENT, specifically the byte cap.
# The upstream default is 4 GiB of in-flight bytes per actor — TWICE this pod's
# entire memory limit. A cap above the limit can never bind: the pod OOMKills
# before admission control ever declines a write, which turns a designed
# backpressure signal (429 + client retry, already implemented in agent-kit's
# OmnigraphClient) into a hard restart of a single-replica Recreate Deployment.
# The cap is only useful below the memory limit, so it has to be set here.
#
# SIZING. The cap is PER ACTOR and there is no global limiter, so total
# in-flight bytes are (concurrent actors x cap) and no single setting can bound
# them. Sized against realistic peak concurrency rather than the worst case:
# a handful of agent sessions plus svc-witan-ci, call it 4 concurrent writers;
# reserving ~768 MiB of the 2 GiB for the server's own working set and Lance
# buffers leaves ~1.25 GiB to share, so ~320 MiB each, rounded down to 256 MiB.
# The pod's memory limit remains the real backstop for a concurrency spike
# beyond that — this bounds the single-runaway-actor case, which is the one
# that actually shows up (a CI reindex streaming a large NDJSON load).
#
# The in-flight COUNT comes down from 16 to 8 for the same reason: against a
# 1-CPU pod, 16 concurrent writes from one actor only queue. An interactive
# witan session issues one write at a time, so 8 is still far above any
# legitimate single-user pattern while halving what one bursty indexer can pin.
#
# These are a deliberate starting point, not a measurement. Revisit from
# observed 429 rates and in-flight-byte peaks once the shared service has
# metrics (tk-observability-for-shared-witan-service-ad3dba) — and note that
# witan's client-side retry cannot read the server's Retry-After header (the
# omnigraph CLI's error path discards response headers), so the sizing signal
# has to come from server-side metrics, not from the client.
PER_ACTOR_INFLIGHT_MAX = 8
PER_ACTOR_BYTES_MAX = 256 * 1024 * 1024

# ── Startup behaviour on an unopenable graph ─────────────────────────────────
#
# DECISION: leave OMNIGRAPH_REQUIRE_ALL_GRAPHS unset (i.e. keep the default
# quarantine behaviour). Setting it was proposed when this cluster served one
# graph, where quarantine's failure mode is a silent brownout — pod Ready,
# /healthz 200, /graphs empty, every real request 404ing — and all-or-nothing
# startup would have made that loud.
#
# That premise no longer holds. build_cluster_graphs now declares `council`,
# `code-bridge`, and one `code-<repo>` graph per managed repo, which is exactly
# the multi-graph condition the original note named as the reversal trigger.
# All-or-nothing would mean one unopenable per-repo code graph — the least
# critical and most numerous kind, rebuildable by a reindex — takes down the
# memory/task/workflow graph the entire team's agents depend on. Quarantine
# gets the blast radius right: a broken code graph costs that repo's code
# lookups and nothing else.
#
# The brownout risk quarantine leaves behind is real but narrower than it was:
# it now only matters for `council` specifically, and the fix is a probe or
# alert that distinguishes "serving" from "serving the graph that matters",
# which /healthz structurally cannot (and /graphs cannot substitute for — it is
# auth-gated behind a graph_list grant on Server::"root", so it is not usable
# as a kubelet probe). That belongs with the service's monitoring, not with a
# startup flag that trades a narrow failure for a broad one — see
# tk-observability-for-shared-witan-service-ad3dba.
#
# REVISIT IF: the cluster ever collapses back to serving `council` alone, or
# the server grows a per-graph health signal a readiness probe can reach
# unauthenticated.

# Fixed ConfigMap name, referenced both by the ConfigMap's own metadata and by
# the Deployment's volume. The Deployment uses this constant rather than
# `cluster_configmap.metadata.name` deliberately: the ConfigMap is replaced on
# every `data` change, which makes that Output unknown at preview time and
# reports the Deployment as replaced too — a replacement that would never
# actually be needed, since the name is pinned and therefore identical across
# the replacement. `depends_on` still orders the two.
CLUSTER_CONFIGMAP_NAME = "omnigraph-cluster-config"

# ── Cluster graph ids ────────────────────────────────────────────────────────
#
# Graph ids track the owning published package rather than the omnigraph
# default branch name: omnigraph's default *branch* is `main`, so a graph also
# called `main` conflates the two. `council` is what witan-council asks for
# (WITAN_MEMORY_GRAPH, default `council` in agent-kit
# mcp/servers/witan/witan/config.py) — a cluster that declares anything else
# serves a graph no client addresses.
COUNCIL_GRAPH_ID = "council"
# Layer 2.5 cross-repo bridge, the deployed analogue of witan-code's local
# `_bridge.omni` store. Fixed id, not derived from any repo.
BRIDGE_GRAPH_ID = "code-bridge"
CODE_GRAPH_PREFIX = "code-"

# Schema filenames, all three baked into the omnigraph-server image at
# CLUSTER_CONFIG_DIR by the image build (agent-kit
# docker/omnigraph-server.Dockerfile). Not sourced here: this Pulumi program
# has no access to agent-kit's working tree at apply time.
COUNCIL_SCHEMA_FILE = "schema.pg"
CODE_SCHEMA_FILE = "code-schema.pg"
BRIDGE_SCHEMA_FILE = "bridge-schema.pg"

_GRAPH_ID_MAX_LEN = 64


def code_graph_id(repo: str) -> str:
    """Canonical cluster graph id for ``repo``'s code graph.

    e.g. ``https://github.com/mitodl/ol-django`` ->
    ``code-github-com-mitodl-ol-django``.

    SHARED CONTRACT — this is a mirror of ``witan_code.config.graph_id``
    (agent-kit, mcp/servers/witan-code/witan_code/config.py), which is what
    selects the ``--graph`` id on the *client* side. This function declares the
    graph the cluster creates. They MUST agree byte-for-byte or a client will
    address a graph that was never created. Any change has to land in lockstep
    on both sides — see agent-kit task
    tk-code-graph-deployment-topology-shared-per-repo-c-cac400.

    Strips the URI scheme, collapses every run of non-alphanumerics to ``-``,
    lowercases, and prefixes ``code-``. omnigraph graph ids must match
    ``^[a-zA-Z0-9-]{1,64}$`` (note: no underscores), so ids that would exceed
    64 characters are truncated and disambiguated with a hash of the full repo
    URI, keeping distinct long repos from colliding.
    """
    body = re.sub(r"(?i)^[a-z][a-z0-9+.-]*://", "", repo)  # strip scheme
    body = re.sub(r"[^a-zA-Z0-9]+", "-", body).strip("-").lower()
    candidate = f"{CODE_GRAPH_PREFIX}{body}"
    if len(candidate) <= _GRAPH_ID_MAX_LEN:
        return candidate
    digest = hashlib.sha256(repo.encode()).hexdigest()[:8]
    keep = _GRAPH_ID_MAX_LEN - len(CODE_GRAPH_PREFIX) - len(digest) - 1
    return f"{CODE_GRAPH_PREFIX}{body[:keep].strip('-')}-{digest}"


def validate_storage_prefix(prefix: str | None) -> str:
    """Normalize and check ``omnigraph:storage_prefix``; return "" when unset.

    The storage root is normally the bucket root; a prefix moves it to
    ``s3://<bucket>/<prefix>`` for a storage-format migration (see
    ``docs/omnigraph-storage-format-upgrade-runbook.md``).

    Validated up front because every failure downstream of here is silent:
    ``omnigraph cluster validate`` accepts *any* storage string, including an
    empty one, so a malformed root is never caught by the tooling — the
    migration just rebuilds the graphs somewhere nobody is looking, and
    ``load`` reports success on top of it.

    Raises ``ValueError`` on a leading/trailing ``/`` (it is joined as
    ``s3://<bucket>/<prefix>``) or on anything that is not a single
    ``[A-Za-z0-9._-]`` path segment. That last rule is what catches an
    unsubstituted ``fmt<N>`` copied out of the runbook: ``<`` and ``>`` are
    legal in S3 object keys, so it would otherwise become a real prefix.
    """
    cleaned = (prefix or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("/") or cleaned.endswith("/"):
        msg = (
            f"omnigraph:storage_prefix must not start or end with '/': "
            f"{cleaned!r}. It is joined as s3://<bucket>/<prefix>."
        )
        raise ValueError(msg)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cleaned):
        msg = (
            f"omnigraph:storage_prefix must be a single path segment of "
            f"[A-Za-z0-9._-] starting alphanumeric: {cleaned!r}. An "
            "unsubstituted placeholder such as 'fmt<N>' lands here."
        )
        raise ValueError(msg)
    return cleaned


def storage_uri_for(bucket: str, prefix: str) -> str:
    """Cluster storage root for ``bucket``, optionally under ``prefix``.

    Kept separate from the ``Output.apply`` that calls it so the join is
    testable — an off-by-one slash here silently relocates every graph.
    """
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"


def build_cluster_graphs(managed_repos: list[str]) -> dict[str, dict[str, str]]:
    """Build the ``graphs:`` block of cluster.yaml for ``managed_repos``.

    One graph per repo rather than one shared code graph (ADR-0009 follow-up,
    decided: option A), matching the per-repo model witan-code already uses
    locally. Each is a self-contained Lance store under
    ``s3://<bucket>/graphs/<graph-id>.omni/``, so per-repo isolation costs
    nothing beyond the entry here, and per-user/per-session WIP branches live
    inside their own repo's graph.

    Adding a repo is deliberately a config change rather than ad-hoc
    self-creation by a client: an unmanaged repo should fail to resolve rather
    than silently mint a graph nobody provisioned or backs up. The
    ``cluster apply`` and server restart it requires are both automatic — the
    converge Job and the Deployment's pod template share one config hash, so
    editing this list creates the graph and restarts the server into it in the
    same deploy.

    Raises ``ValueError`` on a duplicate derived id — two repo URIs that
    normalize to the same graph id would otherwise silently share one store.
    """
    graphs: dict[str, dict[str, str]] = {
        COUNCIL_GRAPH_ID: {"schema": COUNCIL_SCHEMA_FILE},
        BRIDGE_GRAPH_ID: {"schema": BRIDGE_SCHEMA_FILE},
    }
    for repo in managed_repos:
        graph = code_graph_id(repo)
        if graph in graphs:
            msg = (
                f"Duplicate cluster graph id {graph!r} derived from repo "
                f"{repo!r}. Two managed repos normalize to the same id, which "
                "would silently share one store."
            )
            raise ValueError(msg)
        graphs[graph] = {"schema": CODE_SCHEMA_FILE}
    return graphs


def omnigraph_server_addr(namespace: str) -> str:
    """Return the in-cluster HTTP address witan's MCPServer talks to."""
    return (
        f"http://{OMNIGRAPH_SERVER_SERVICE_NAME}.{namespace}"
        f".svc.cluster.local:{OMNIGRAPH_SERVER_PORT}"
    )


class OmnigraphDataTier(NamedTuple):
    """Handles to the provisioned data-tier resources for depends_on wiring."""

    bucket: OLBucket
    image_repository: str
    service: kubernetes.core.v1.Service
    deployment: kubernetes.apps.v1.Deployment
    cluster_apply_job: kubernetes.batch.v1.Job
    maintenance: OmnigraphMaintenance


def create_data_tier(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    aws_config: AWSBase,
    auth_binding: OLEKSAuthBinding,
    actor_tokens_secret_name: str,
    actor_tokens_secret: OLVaultK8SSecret,
    managed_repos: list[str],
    optimize_schedule: str,
    cleanup_schedule: str,
    cleanup_older_than: str,
    storage_prefix: str = "",
) -> OmnigraphDataTier:
    """Provision the S3 bucket, IRSA policy, ECR repo, ConfigMap, and Deployment.

    ``storage_prefix`` moves the cluster's storage root to a prefix inside the
    managed bucket (``s3://<bucket>/<prefix>``) instead of the bucket root. It
    exists for the storage-format migration in
    ``docs/omnigraph-storage-format-upgrade-runbook.md``, where the graphs are
    rebuilt under a new root and the cluster is then repointed at it, leaving
    the old root intact as the rollback. Empty (the default) means the bucket
    root, which is the steady state.
    """
    # The bucket is named for its tenant (witan's graphs), not the omnigraph
    # service — omnigraph is generic and a future second instance would get its
    # own tenant-named bucket rather than colliding on an omnigraph-named one.
    bucket_name = f"ol-data-witan-{stack_info.env_suffix}"
    omnigraph_bucket = OLBucket(
        f"omnigraph-bucket-{stack_info.env_suffix}",
        S3BucketConfig(
            bucket_name=bucket_name,
            versioning_enabled=True,
            tags=aws_config.tags,
        ),
    )

    # The bucket ARN is only known as an Output, so the IAM policy is built
    # after the fact and attached to the IRSA role directly — the same
    # pattern clickhouse/__main__.py uses for its own OLBucket-backed IRSA
    # grant (iam_policy_document=None on the OLEKSAuthBinding config).
    omnigraph_s3_policy_json: Output[str] = Output.all(
        bucket_arn=omnigraph_bucket.bucket_v2.arn
    ).apply(
        lambda args: json.dumps(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:ListBucket",
                            "s3:GetBucketLocation",
                            "s3:AbortMultipartUpload",
                            "s3:ListMultipartUploadParts",
                        ],
                        "Resource": [
                            args["bucket_arn"],
                            f"{args['bucket_arn']}/*",
                        ],
                    }
                ],
            }
        )
    )
    omnigraph_s3_iam_policy = aws.iam.Policy(
        f"omnigraph-s3-iam-policy-{stack_info.env_suffix}",
        path=f"/ol-data/omnigraph-s3-iam-policy-{stack_info.env_suffix}/",
        policy=omnigraph_s3_policy_json,
    )
    aws.iam.RolePolicyAttachment(
        f"omnigraph-s3-iam-policy-attachment-{stack_info.env_suffix}",
        policy_arn=omnigraph_s3_iam_policy.arn,
        role=auth_binding.irsa_role.name,
    )

    # The ``omnigraph-server`` ECR repository is created (idempotently) by the
    # Concourse build job on every push, not managed here — see this module's
    # docstring. One repo is shared across CI/QA/Production (same AWS
    # account); the image is pinned by digest (OMNIGRAPH_DOCKER_SHA, set by
    # the build job) so a new push actually changes this stage's Deployment
    # pod spec.
    omnigraph_aws_account = aws.get_caller_identity()
    image_repository = (
        f"{omnigraph_aws_account.account_id}.dkr.ecr.{aws_config.region}"
        ".amazonaws.com/omnigraph-server"
    )
    omnigraph_server_image = format_docker_image_ref(image_repository, "OMNIGRAPH")

    # cluster.yaml — the Layer-1 (memory/task/workflow) `council` graph,
    # organization-wide, plus the `code-bridge` graph and one `code-<repo>`
    # graph per managed repo (see build_cluster_graphs).
    # None of the three schema files are sourced here — this Pulumi program has
    # no access to agent-kit's working tree at apply time. All are baked into
    # the omnigraph-server image at build time under ``{CLUSTER_CONFIG_DIR}``
    # by agent-kit's docker/omnigraph-server.Dockerfile. The ConfigMap volume
    # below is mounted with ``sub_path`` so it overlays only the single
    # ``cluster.yaml`` file and leaves those baked-in schemas visible alongside
    # it, rather than replacing the whole directory.
    cluster_graphs = build_cluster_graphs(managed_repos)
    # Storage root. Normally the bucket root; `omnigraph:storage_prefix` moves
    # it to a prefix *inside* that same bucket, which is what a storage-format
    # migration needs (docs/omnigraph-storage-format-upgrade-runbook.md).
    #
    # Deliberately a prefix rather than a full URI: the bucket, its IAM policy
    # and the IRSA grant above stay keyed to the derived name no matter what
    # this is set to. A free-form URI could point the cluster at a bucket
    # nothing has granted access to, and the failure would land mid-migration.
    # `cluster validate` would not catch it either — it accepts any storage
    # string, including an empty one.
    storage_uri: Output[str] = omnigraph_bucket.bucket_v2.bucket.apply(
        lambda name: storage_uri_for(name, storage_prefix)
    )
    cluster_name = f"mitodl-witan-{stack_info.env_suffix.lower()}"
    cluster_yaml_content: Output[str] = storage_uri.apply(
        lambda uri: yaml.dump(
            {
                "version": 1,
                "metadata": {"name": cluster_name},
                "state": {"backend": "cluster"},
                "storage": uri,
                "graphs": cluster_graphs,
            },
            sort_keys=False,
        )
    )
    cluster_configmap = kubernetes.core.v1.ConfigMap(
        f"omnigraph-omnigraph-cluster-config-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=CLUSTER_CONFIGMAP_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={"cluster.yaml": cluster_yaml_content},
        # pulumi-kubernetes treats a ConfigMap `data` change as force-new, and
        # normally relies on auto-naming so the replacement can be created
        # before the old one is deleted. `metadata.name` is pinned here (the
        # Deployment's volume references it by name), which disables
        # auto-naming — so the default create-before-delete collides with the
        # live object and the update fails with "configmaps
        # omnigraph-cluster-config already exists". Delete first instead.
        #
        # Safe despite the Deployment mounting this: the mount uses `sub_path`,
        # and sub-path volume mounts never receive ConfigMap updates, so the
        # running pod holds its own copy of cluster.yaml either way. A changed
        # graph list reaches the server on the pod restart that the config-hash
        # annotation on the Deployment's pod template forces — same hash the
        # converge Job carries, so apply and restart always move together.
        opts=ResourceOptions(delete_before_replace=True),
    )

    # ── Pre-deploy schema convergence ────────────────────────────────────────
    #
    # `omnigraph cluster apply` creates any newly-declared graph and applies
    # schema updates to the existing ones, from the schemas baked into this very
    # image. It is the *only* thing that reconciles a live graph with a changed
    # schema: a graph is created with whatever schema it was born with, and
    # nothing re-reads the file afterwards. Without this, adding a field in
    # agent-kit (e.g. `WorkflowSession.superseded_by`) ships an image whose code
    # selects a column the store has never heard of, and every read of that type
    # fails against the deployed graph.
    #
    # Ordered before the Deployment (its depends_on, below) because omnigraph
    # only picks up the applied revision on an `omnigraph-server --cluster`
    # restart — converge first, then let the rollout restart into it. This is
    # the "cluster apply step" the ConfigMap comment above already refers to.
    #
    # Runs as a Job rather than an initContainer so it executes once per deploy
    # rather than once per pod restart (crashloop, eviction, node drain), and so
    # a convergence failure surfaces as a failed Job that blocks the rollout
    # instead of a pod stuck in Init.
    # Full digest, matching the `ol.mit.edu/config-hash` convention in
    # components/services/k8s.py — an annotation value has no length pressure,
    # so there is nothing to buy by truncating.
    cluster_apply_hash: Output[str] = Output.all(
        cluster_yaml=cluster_yaml_content, image=omnigraph_server_image
    ).apply(
        lambda args: hashlib.sha256(
            f"{args['cluster_yaml']}\n{args['image']}".encode()
        ).hexdigest()
    )
    cluster_apply_job = kubernetes.batch.v1.Job(
        f"omnigraph-cluster-apply-{stack_info.env_suffix}",
        # Deliberately unnamed (Pulumi auto-naming): a Job's pod template is
        # immutable, so every change here is a replacement, and auto-naming lets
        # the new Job be created before the old one is removed.
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            # Converging is idempotent, so a couple of retries costs nothing and
            # rides out a transient S3 or IRSA-credential hiccup.
            backoff_limit=2,
            # Keep a completed Job around for a day so its logs are readable
            # after a deploy, then let the TTL controller reap it.
            ttl_seconds_after_finished=86400,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={
                        **k8s_global_labels,
                        "app.kubernetes.io/name": "omnigraph-cluster-apply",
                    },
                    # Forces a new Job whenever the declared graph list OR the
                    # image (and therefore the baked schemas) changes. Without
                    # it, adding a repo to cluster.yaml would leave this Job's
                    # spec byte-identical and the graph would never be created.
                    annotations={"ol.mit.edu/config-hash": cluster_apply_hash},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    restart_policy="Never",
                    # Same IRSA identity as the server: this writes the graphs'
                    # Lance stores directly in S3, not through the server.
                    service_account_name=OMNIGRAPH_SERVICE_ACCOUNT_NAME,
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="cluster-apply",
                            image=omnigraph_server_image,
                            # Override the image's server entrypoint script —
                            # this runs the `omnigraph` CLI baked alongside it.
                            command=["omnigraph"],
                            args=[
                                "cluster",
                                "apply",
                                "--config",
                                CLUSTER_CONFIG_DIR,
                                "--as",
                                CLUSTER_APPLY_ACTOR,
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="AWS_REGION", value=aws_config.region
                                ),
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "100m", "memory": "256Mi"},
                                limits={"cpu": "1", "memory": "1Gi"},
                            ),
                            volume_mounts=[
                                # sub_path for the same reason the Deployment
                                # uses it: overlay only cluster.yaml and leave
                                # the image's baked-in schema files visible
                                # alongside it. `cluster apply` reads both.
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="cluster-config",
                                    mount_path=f"{CLUSTER_CONFIG_DIR}/cluster.yaml",
                                    sub_path="cluster.yaml",
                                    read_only=True,
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="cluster-config",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name=CLUSTER_CONFIGMAP_NAME,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[
                cluster_configmap,
                *auth_binding.irsa_service_accounts,
            ]
        ),
    )

    omnigraph_pod_labels = {
        **k8s_global_labels,
        "app.kubernetes.io/name": OMNIGRAPH_SERVER_SERVICE_NAME,
    }
    omnigraph_deployment = kubernetes.apps.v1.Deployment(
        f"omnigraph-omnigraph-server-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=OMNIGRAPH_SERVER_SERVICE_NAME,
            namespace=namespace,
            labels=omnigraph_pod_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            # Single writer, deliberately. omnigraph's own deployment contract
            # documents multi-replica serving off shared storage as "unvalidated"
            # and lists it under Don't; concurrent writers rely on a single
            # server's in-process CAS, not cross-process coordination. Do NOT add
            # an HPA or bump replicas without validating multi-writer safety.
            replicas=1,
            # Recreate, NOT the default RollingUpdate: storage is
            # strict-single-version ("a binary reads exactly one storage-format
            # version"; a mixed fleet writing one graph is unsupported), so a
            # rollout must never run the old and new image against the same S3
            # store at once. Recreate tears down the old pod before starting the
            # new one, eliminating that overlap window. (A rollout that actually
            # bumps the storage format still needs the offline export/rebuild
            # runbook — Recreate only covers same-format restarts.)
            strategy=kubernetes.apps.v1.DeploymentStrategyArgs(type="Recreate"),
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app.kubernetes.io/name": OMNIGRAPH_SERVER_SERVICE_NAME}
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=omnigraph_pod_labels,
                    # The same hash the converge Job carries, so the server is
                    # restarted by any change the Job acted on — not just the
                    # ones that happen to change the image.
                    #
                    # omnigraph serves the revision it read at boot, and the
                    # cluster.yaml mount is a sub_path (which never receives
                    # ConfigMap updates), so without this a deploy that only
                    # edits cluster.yaml — adding a managed repo, say — would
                    # converge the new graph into S3 and then keep serving a
                    # config that has never heard of it. Deploy-time
                    # consistency is worth the restart: this Deployment is
                    # single-replica `Recreate`, so a config change is a brief
                    # data-tier outage by construction.
                    annotations={"ol.mit.edu/config-hash": cluster_apply_hash},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=OMNIGRAPH_SERVICE_ACCOUNT_NAME,
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="omnigraph-server",
                            image=omnigraph_server_image,
                            args=[
                                "--cluster",
                                CLUSTER_CONFIG_DIR,
                                "--bind",
                                f"0.0.0.0:{OMNIGRAPH_SERVER_PORT}",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="AWS_REGION", value=aws_config.region
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_SERVER_BEARER_TOKENS_FILE",
                                    value=(
                                        f"{ACTOR_TOKENS_MOUNT_PATH}/"
                                        f"{ACTOR_TOKENS_FILENAME}"
                                    ),
                                ),
                                # Per-actor admission caps, set deliberately
                                # rather than inherited — the upstream byte
                                # default is twice this pod's memory limit and
                                # therefore unreachable. See the constants.
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_PER_ACTOR_INFLIGHT_MAX",
                                    value=str(PER_ACTOR_INFLIGHT_MAX),
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_PER_ACTOR_BYTES_MAX",
                                    value=str(PER_ACTOR_BYTES_MAX),
                                ),
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=OMNIGRAPH_SERVER_PORT
                                )
                            ],
                            # omnigraph documents `/healthz` (flat, never
                            # auth-gated) as the health-check endpoint; use it
                            # over a bare TCP check so the probe reflects the
                            # server actually being up, not just the port being
                            # bound.
                            #
                            # DELAYS ARE SIZED FROM A MEASURED BOOT, not picked.
                            # The entrypoint converges the cluster catalog
                            # *before* the server binds :8080, and that converge
                            # re-observes every declared graph over S3. Measured
                            # 2026-08-05 with 17 graphs, from the container's
                            # first log line to `serving omnigraph bind=`:
                            #
                            #     CI 17.2s | QA 19.8s | Production 17.2s
                            #
                            # ~95% of which is the converge. At the previous
                            # values (readiness 5, liveness 15) both probes were
                            # guaranteed to fire against a closed port on every
                            # start in every environment — the connection-refused
                            # Unhealthy events on a healthy boot were this, not a
                            # sick server. Only failureThreshold=3 kept it from
                            # being a crashloop.
                            #
                            # The two probes are sized against that measurement
                            # for DIFFERENT reasons, and only one of them
                            # carries headroom:
                            #
                            # - readiness sits just past the slowest boot (20s
                            #   vs 19.8s). Deliberate: a failed readiness probe
                            #   costs nothing but a 5s retry, and every second
                            #   spent out of the Service is restart outage. It
                            #   is allowed to be tight.
                            # - liveness carries the headroom (60s, 3x), because
                            #   its failure mode is killing a healthy pod.
                            #
                            # The converge cost scales with the declared graph
                            # count (~1.2s/graph), so as `managed_repos` grows
                            # readiness will start probing early again — noisy
                            # but harmless — and liveness eats into its 3x. A
                            # fixed delay cannot track that growth; a
                            # startupProbe is the mechanism that does, and is
                            # the right follow-up if the graph list keeps
                            # expanding.
                            readiness_probe=kubernetes.core.v1.ProbeArgs(
                                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                    path="/healthz",
                                    port=OMNIGRAPH_SERVER_PORT,
                                ),
                                # First probe just past the measured boot, then
                                # poll fast: this is what ends the restart
                                # outage, so the gap between "bound" and "in the
                                # Service" should be small.
                                initial_delay_seconds=20,
                                period_seconds=5,
                            ),
                            liveness_probe=kubernetes.core.v1.ProbeArgs(
                                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                    path="/healthz",
                                    port=OMNIGRAPH_SERVER_PORT,
                                ),
                                # 3x the slowest measured boot. With
                                # period_seconds=20 and the default
                                # failureThreshold=3, a genuinely wedged server
                                # is still killed by ~100s.
                                initial_delay_seconds=60,
                                period_seconds=20,
                            ),
                            # The memory limit is what PER_ACTOR_BYTES_MAX is
                            # sized against — change one and revisit the other.
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "250m", "memory": "512Mi"},
                                limits={"cpu": "1", "memory": SERVER_MEMORY_LIMIT},
                            ),
                            volume_mounts=[
                                # sub_path overlays only cluster.yaml, leaving
                                # the image's baked-in schema.pg (same dir)
                                # visible — see the comment above the
                                # ConfigMap definition.
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="cluster-config",
                                    mount_path=f"{CLUSTER_CONFIG_DIR}/cluster.yaml",
                                    sub_path="cluster.yaml",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="actor-tokens",
                                    mount_path=ACTOR_TOKENS_MOUNT_PATH,
                                    read_only=True,
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="cluster-config",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name=CLUSTER_CONFIGMAP_NAME,
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="actor-tokens",
                            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                                secret_name=actor_tokens_secret_name,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[
                cluster_configmap,
                actor_tokens_secret,
                # Converge the graphs' schemas before the server restarts into
                # them — omnigraph only serves the applied revision after a
                # restart, so this ordering is what makes the new schema live.
                cluster_apply_job,
                # The pod's service_account_name is the IRSA SA this stack
                # creates via auth_binding (create_irsa_service_account=True);
                # wait for it so the initial apply doesn't transiently fail
                # with `serviceaccount "omnigraph-server" not found`.
                *auth_binding.irsa_service_accounts,
            ]
        ),
    )

    omnigraph_service = kubernetes.core.v1.Service(
        f"omnigraph-omnigraph-server-service-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=OMNIGRAPH_SERVER_SERVICE_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            selector={"app.kubernetes.io/name": OMNIGRAPH_SERVER_SERVICE_NAME},
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="http",
                    port=OMNIGRAPH_SERVER_PORT,
                    target_port=OMNIGRAPH_SERVER_PORT,
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
        opts=ResourceOptions(depends_on=[omnigraph_deployment]),
    )

    # Scheduled compaction and version GC against the S3 store directly. Swept
    # per graph over exactly the ids cluster.yaml declares — the same
    # `cluster_graphs` dict rendered into the ConfigMap above, so a newly
    # managed repo joins the sweep in the deploy that creates its graph.
    #
    # Ordered behind the converge Job because a graph that has not been applied
    # yet is a hard error for `optimize --graph <id>` ("graph `X` is not applied
    # in cluster ..."), which would fail the first sweep of every new graph.
    omnigraph_maintenance = create_maintenance(
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        image=omnigraph_server_image,
        service_account_name=OMNIGRAPH_SERVICE_ACCOUNT_NAME,
        aws_region=aws_config.region,
        storage_uri=storage_uri,
        maintenance_actor=CLUSTER_APPLY_ACTOR,
        graph_ids=list(cluster_graphs),
        optimize_schedule=optimize_schedule,
        cleanup_schedule=cleanup_schedule,
        cleanup_older_than=cleanup_older_than,
        depends_on=[cluster_apply_job, *auth_binding.irsa_service_accounts],
    )

    return OmnigraphDataTier(
        bucket=omnigraph_bucket,
        image_repository=image_repository,
        service=omnigraph_service,
        deployment=omnigraph_deployment,
        cluster_apply_job=cluster_apply_job,
        maintenance=omnigraph_maintenance,
    )
