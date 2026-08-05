"""The break-glass maintenance pod template: a suspended CronJob.

Agent-kit ADR-0005 path (b) reserves a class of operations for an in-cluster,
service-authenticated path: ``witan migrate schema`` / ``migrate storage`` /
``migrate merge``, and cross-actor debugging. They are deliberately **not**
``@mcp.tool`` — the remote MCP proxy refuses them outright — because they operate
on the store as a whole and have no per-user identity to scope. ``migrations.py``
runs the two idempotent backfills on every deploy; this module covers the rest,
the ones a human decides to run.

WHY A SUSPENDED CRONJOB AND NOT A JOB, AND NOT A RUNBOOK ``kubectl run``

The thing an operator needs at 2am is not a schedule — it is the *pod spec*: the
right image digest, the right ClusterIP address, the right graph id, the right
token from the right Secret, and a service account that can read it. A Job
declared in Pulumi would run once at deploy time, which is exactly wrong for
break-glass. A ``kubectl run`` line in a runbook would have to restate all five,
and a runbook that restates a pod spec is a runbook that drifts from it — the
digest alone makes it wrong on the next deploy.

A CronJob with ``suspend: true`` is the shape that stores a pod template without
running it. Kubernetes has a first-class verb for instantiating one on demand::

    kubectl -n witan create job witan-bg-$(date +%s) \\
        --from=cronjob/witan-break-glass
    kubectl -n witan exec -it job/witan-bg-<...> -- witan migrate schema

The schedule is required by the API and never fires; it is set to a date that
cannot occur (see ``NEVER_SCHEDULE``) so that a bug or a manual ``kubectl patch``
un-suspending it still does not silently start running migrations on a timer.

WHY IT SLEEPS BY DEFAULT, AND WHY THAT IS THE ONLY WAY IN

The container's default command is a ``sleep``, so the pod comes up idle and the
operator runs the real command through ``kubectl exec`` — the bastion-pod half of
ADR-0005 path (b). That is not merely the safe default, it is the only shape
``--from`` supports: ``kubectl create job --from=cronjob/x -- <command>`` is
rejected outright (``error: cannot specify --from and command``, verified against
kubectl rather than inferred), and a Job's pod template is immutable once
created, so there is no patching it afterwards either.

An unattended one-shot is still possible, by rendering the Job and overriding the
command before it is submitted::

    kubectl -n witan create job witan-bg-$(date +%s) --from=cronjob/witan-break-glass \\
        --dry-run=client -o json \\
      | jq '.spec.template.spec.containers[0].command = ["witan","migrate","schema"]' \\
      | kubectl -n witan create -f -

Worth knowing about, but the interactive path is the one to reach for first: these
are operations somebody is watching.

WHAT IT CANNOT DO

This pod authenticates as ``svc-witan-admin``, whose Cedar grant is read + schema
on the code and bridge graphs and read/write + schema on memory (agent-kit
``mcp/servers/witan/policy/``). It cannot reindex a code graph, promote a WIP
view into ``main``, or delete anybody's branch view. It also cannot run
``omnigraph repair``/``optimize``/``cleanup``: those are direct-storage commands
that need the S3 credentials this namespace deliberately does not have — they run
as the omnigraph stack's own CronJobs, gated by IAM instead of Cedar. See
``docs/witan-admin-break-glass-runbook.md``.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.lib.pulumi_helper import StackInfo

CRONJOB_NAME = "witan-break-glass"

# 31 February. The API validates the *format*, not that the date can occur, so
# this parses fine and matches nothing. Belt and braces on top of
# `suspend: true`: un-suspending by accident (a stray `kubectl patch`, a future
# refactor that flips the field) still starts nothing.
NEVER_SCHEDULE = "0 0 31 2 *"

# Four hours: longer than any plausible investigation, shorter than a day, so a
# pod somebody walked away from cleans itself up. This is also the pod's whole
# lifetime in the normal (exec) flow — `kubectl create job --from=` copies the
# template verbatim, and the migration runs *inside* this sleep rather than
# replacing it, so the window has to cover the work as well as the thinking.
# `kubectl delete job` is how you end one early.
DEFAULT_IDLE_SECONDS = 14400

# One attempt. Every operation reached through this pod is either idempotent (a
# schema apply) or one a human is watching (a merge, a storage rebuild); an
# automatic retry of a half-finished manual migration is the last thing anybody
# wants at 2am.
BACKOFF_LIMIT = 0

# Keep finished break-glass pods around for a week, far longer than the
# migration Job's day: these are the record of a manual intervention, and
# `kubectl logs` on them is how the next person finds out what was done.
TTL_SECONDS_AFTER_FINISHED = 604800


def create_break_glass_cronjob(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    witan_image: str | Output[str],
    omnigraph_server_addr: str | Output[str],
    council_graph_id: str | Output[str],
    admin_actor_id: str,
    admin_token_secret_name: str,
    admin_token_secret_key: str,
    admin_token_secret: Resource,
) -> kubernetes.batch.v1.CronJob:
    """Declare the suspended break-glass pod template. See the module docstring."""
    return kubernetes.batch.v1.CronJob(
        f"witan-break-glass-{stack_info.env_suffix}",
        # Explicitly named, unlike the migration Job's Pulumi auto-naming: the
        # whole point is that a human types `--from=cronjob/witan-break-glass`
        # from a runbook, which needs a name that does not change on every
        # deploy.
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=CRONJOB_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
            annotations={
                "ol.mit.edu/purpose": (
                    "Break-glass maintenance template (ADR-0005 path b). Never "
                    "scheduled; instantiate with `kubectl create job "
                    f"--from=cronjob/{CRONJOB_NAME}`, then `kubectl exec` into "
                    "it. See docs/witan-admin-break-glass-runbook.md."
                ),
            },
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=NEVER_SCHEDULE,
            suspend=True,
            # A manual `create job --from=` is not bound by this, so it does not
            # stop an operator from starting a second one deliberately; it is
            # here so that an un-suspended CronJob cannot pile runs on top of an
            # in-flight migration.
            concurrency_policy="Forbid",
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=k8s_global_labels,
                ),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=BACKOFF_LIMIT,
                    ttl_seconds_after_finished=TTL_SECONDS_AFTER_FINISHED,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels={
                                **k8s_global_labels,
                                "app.kubernetes.io/name": CRONJOB_NAME,
                            },
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="witan",
                                    image=witan_image,
                                    # Overrides the image's server entrypoint
                                    # with an idle shell — see DEFAULT_IDLE_
                                    # SECONDS and the module docstring on why the
                                    # default is a shell and not a migration.
                                    command=[
                                        "/bin/sh",
                                        "-c",
                                        f"sleep {DEFAULT_IDLE_SECONDS}",
                                    ],
                                    env=[
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_MEMORY_URI",
                                            value=omnigraph_server_addr,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_MEMORY_GRAPH",
                                            value=council_graph_id,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_MEMORY_TOKEN",
                                            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                                    name=admin_token_secret_name,
                                                    key=admin_token_secret_key,
                                                )
                                            ),
                                        ),
                                        # Provenance for anything a manual
                                        # migration writes; same reasoning as
                                        # migrations.py.
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="WITAN_AUTHOR",
                                            value=admin_actor_id,
                                        ),
                                        # WITAN_REMOTE_URL stays unset on
                                        # purpose: setting it would route these
                                        # commands at the MCP tier, which
                                        # refuses every one of them as
                                        # admin-only (witan/remote/proxy.py
                                        # `_ADMIN_ONLY`). This pod IS the path
                                        # they are reserved for.
                                    ],
                                    # Sized for the one operation here that is
                                    # not trivial: `migrate storage` rebuilds a
                                    # store through memory. Still below the
                                    # data tier's own limit — this shares a node
                                    # pool with the server, and a maintenance
                                    # pod losing an eviction race to the serving
                                    # pod is the correct outcome.
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests={"cpu": "100m", "memory": "256Mi"},
                                        limits={"cpu": "1", "memory": "1Gi"},
                                    ),
                                )
                            ],
                        ),
                    ),
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[admin_token_secret]),
    )
