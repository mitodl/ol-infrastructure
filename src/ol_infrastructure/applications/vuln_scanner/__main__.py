"""vuln-scanner: DAST scanning of internal web apps, starting with MIT Learn QA.

Runs OWASP ZAP and Nuclei as weekly Kubernetes CronJobs against the targets
listed in `vuln_scanner:targets` config, on the operations cluster's QA
stack (deliberately QA, not Production -- see the plan doc's rationale: this
tool generates active attack payloads and holds real S3-write +
Security-Hub-import permissions, and its targets are themselves QA
endpoints). Each scan's raw report is uploaded to S3 and its findings are
imported into AWS Security Hub via ASFF (BatchImportFindings), with a
diff-and-archive step so fixed findings actually clear instead of sitting
`RecordState: ACTIVE` forever -- see reporter/reporter.py.

Single environment (QA) by design, mirroring gwarek's own single-environment
("Production" for gwarek, "QA" here) internal-tool pattern -- this is not on
the org's full CI/QA/Production Concourse promotion pipeline.
"""

from pathlib import Path

import yaml
from pulumi import Config, Resource, ResourceOptions
from pulumi_aws import get_caller_identity
from pulumi_kubernetes import batch, core, meta

from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    format_docker_image_ref,
    make_stack_reference,
    parse_stack,
)

stack_info = parse_stack()
vuln_scanner_config = Config("vuln_scanner")
aws_account = get_caller_identity()

cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

vuln_scanner_namespace = "vuln-scanner"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(vuln_scanner_namespace, ns)
)

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.vuln_scanner,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
application_labels = {**k8s_global_labels.model_dump(), "app": "vuln-scanner"}

targets = vuln_scanner_config.require_object("targets")
# .get() (not `or "baseline"`) on purpose: `or` would coalesce an
# explicit empty string to the same thing as "unset" *before* the check
# below ever saw it, silently downgrading a cleared config value to
# baseline instead of rejecting it as invalid -- caught in review, since
# an earlier version of this exact code did that.
zap_scan_type = vuln_scanner_config.get("zap_scan_type")
if zap_scan_type is None:
    zap_scan_type = "baseline"
elif zap_scan_type not in {"baseline", "full"}:
    msg = (
        f"vuln_scanner:zap_scan_type must be 'baseline' or 'full', "
        f"got {zap_scan_type!r}"
    )
    raise ValueError(msg)

# ---------------------------------------------------------------------------
# S3 bucket for raw ZAP/Nuclei reports
# ---------------------------------------------------------------------------
report_bucket_name = f"ol-vuln-scanner-reports-{stack_info.env_suffix}"
report_bucket = OLBucket(
    "vuln-scanner-reports-bucket",
    config=S3BucketConfig(
        bucket_name=report_bucket_name,
        versioning_enabled=True,
        tags=aws_config.tags,
    ),
)


def _bucket_arns(bucket_name: str) -> list[str]:
    # Built from the plain bucket name string, not the OLBucket resource's
    # Output -- mirrors gwarek's identical helper/rationale
    # (applications/gwarek/__main__.py:292-299): IAM policy documents get
    # json.dumps()'d, which can't serialize an unresolved Output.
    return [f"arn:aws:s3:::{bucket_name}", f"arn:aws:s3:::{bucket_name}/*"]


# ---------------------------------------------------------------------------
# EKS auth binding: IRSA for S3 (report upload) + Security Hub (finding
# import/archive) access. No Vault secret is needed for v1 (no authenticated
# scanning yet) -- vuln_scanner_policy.hcl is intentionally an empty
# placeholder, required only because OLEKSAuthBindingConfig always wires up
# a Vault k8s auth binding alongside IRSA.
# ---------------------------------------------------------------------------
vuln_scanner_irsa_service_account_name = "vuln-scanner"

vuln_scanner_iam_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:ListBucket"],
            "Resource": _bucket_arns(report_bucket_name),
        },
        {
            # Security Hub's finding import/update/read actions have no
            # resource-level ARN scoping in IAM -- the account-wide
            # `Resource: "*"` here is not a mistake, it's the only shape
            # these actions support. Blast-radius control comes from which
            # role can assume this (IRSA, this one service account/
            # namespace only), not from an IAM Resource restriction.
            "Effect": "Allow",
            "Action": [
                "securityhub:BatchImportFindings",
                "securityhub:BatchUpdateFindings",
                "securityhub:GetFindings",
            ],
            "Resource": "*",
        },
    ],
}

vuln_scanner_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="vuln-scanner",
        namespace=vuln_scanner_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=vuln_scanner_iam_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("vuln_scanner_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=vuln_scanner_irsa_service_account_name,
        create_irsa_service_account=True,
        vault_sync_service_account_names=["vuln-scanner-vault"],
        k8s_labels=k8s_global_labels,
    )
)

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
reporter_image_repository = (
    f"{aws_account.account_id}.dkr.ecr.{aws_config.region}.amazonaws.com"
    "/vuln-scanner-reporter"
)
reporter_image = format_docker_image_ref(
    reporter_image_repository, "VULN_SCANNER_REPORTER"
)

# Pinned by digest, not floating tags, so what runs is deliberate and
# reviewable -- captured via `docker buildx imagetools inspect <image>:<tag>`
# against the real ghcr.io/zaproxy/zaproxy:stable and
# projectdiscovery/nuclei:latest tags on 2026-08-28.
#
# NOT currently Renovate-tracked, unlike this repo's usual pattern
# (src/bridge/lib/versions.py's `# renovate: datasource=docker depName=...`
# convention) -- that registry is tag-based (`VERSION = "0.7.7"`-style)
# throughout, with no existing digest-pin precedent to follow, and a bare
# `image@sha256:...` with no tag reference doesn't give Renovate's docker
# datasource a tag to diff the digest against anyway. Bumping either of
# these currently means manually re-running `imagetools inspect` and
# editing here -- a real gap, not a solved one; worth a follow-up to add a
# proper Renovate custom manager for this file if these need to move
# automatically rather than by manual review.
ZAP_IMAGE = "ghcr.io/zaproxy/zaproxy@sha256:781a2bdaea47324e7bab583e2263f21d257b0aee61ed51521a5be45f5f5081ef"  # noqa: E501
NUCLEI_IMAGE = "projectdiscovery/nuclei@sha256:582d5546902e67052097cb2d07296c642d50a1afc5e44623cb038845df9a32eb"  # noqa: E501

ZAP_REPORT_DIR = "/zap/wrk/report"
ZAP_REPORT_FILENAME = "report.json"
NUCLEI_REPORT_PATH = "/shared/nuclei-report.jsonl"
NUCLEI_TEMPLATES_DIR = "/nuclei-templates"


def _stagger_schedule(base_hour: int, index: int) -> str:
    """Weekly cron schedule staggered by 10 minutes per target index.

    Wraps into later hours via divmod rather than letting the minute field
    grow unbounded -- `index * 10` alone becomes an invalid ">59" minute
    field once a 7th target is configured. At 6 targets per hour this has
    headroom for ~144 targets before the hour field itself could overflow
    past 23, far beyond any realistic target list.
    """
    hour_offset, minute = divmod(index * 10, 60)
    return f"{minute} {base_hour + hour_offset} * * 0"


def _hardened_security_context(
    *, run_as_user: int | None = None
) -> core.v1.SecurityContextArgs:
    # run_as_user is left unset (None) for our own reporter image, which
    # already runs as a known non-root user via its own Dockerfile `USER`
    # directive -- forcing a specific UID here could mismatch file ownership
    # baked in at build time. It's passed explicitly for the third-party
    # ZAP/Nuclei images below: their upstream Dockerfiles are unverified
    # (still floating tags, not yet pinned by digest -- see the note above),
    # so run_as_non_root=True alone would depend on guessing whichever user
    # each image happens to default to, and Nuclei's official image has
    # historically had no USER directive at all (defaults to root), which
    # would fail kubelet's non-root check with no user pinned.
    return core.v1.SecurityContextArgs(
        run_as_non_root=True,
        run_as_user=run_as_user,
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        capabilities=core.v1.CapabilitiesArgs(drop=["ALL"]),
    )


def _zap_automation_plan(
    target_name: str,
    target_url: str,
    openapi_schema_url: str | None,
    scan_type: str,
) -> str:
    """Build a ZAP Automation Framework plan (YAML) for one target.

    Uses the `openapi` job to drive endpoint discovery when a spec URL is
    known -- ZAP's default spider is built for following HTML `<a href>`
    links and finds almost nothing against a bare JSON API root, which would
    make even a "successful" baseline run a false-negative machine. See the
    plan doc: confirmed `api.rc.learn.mit.edu` publishes a real OpenAPI 3.0.3
    spec at `/api/v1/schema/`.
    """
    if openapi_schema_url:
        discovery_jobs = [
            {
                "type": "openapi",
                "parameters": {
                    "apiUrl": openapi_schema_url,
                    "targetUrl": target_url,
                },
            }
        ]
    else:
        # No known OpenAPI spec for this target: falls back to ZAP's
        # HTML-oriented spider, which discovers little against a bare JSON
        # API. Confirm a spec exists (Verification section of the plan doc)
        # before trusting a "successful" run that took this branch.
        discovery_jobs = [
            {"type": "spider", "parameters": {"url": target_url, "maxDuration": 5}}
        ]

    jobs = [
        *discovery_jobs,
        {"type": "passiveScan-wait", "parameters": {"maxDuration": 10}},
    ]

    if scan_type == "full":
        jobs.append(
            {
                "type": "activeScan",
                "parameters": {
                    "context": "Default Context",
                    # Politeness: MIT Learn's APISIX gateway runs
                    # chaitin-waf/limit-count/limit-req (see plan doc) --
                    # an unthrottled scan risks getting blocked mid-run
                    # (an incomplete report reading as "clean") or
                    # tripping alerting that isn't expecting scan traffic.
                    "threadPerHost": 2,
                    "delayInMs": 250,
                    "maxRuleDurationInMins": 5,
                    "maxScanDurationInMins": 60,
                },
            }
        )

    jobs.append(
        {
            "type": "report",
            "parameters": {
                "template": "risk-confidence-json",
                "reportDir": ZAP_REPORT_DIR,
                "reportFile": Path(ZAP_REPORT_FILENAME).stem,
                "reportTitle": f"ZAP scan of {target_name}",
            },
        }
    )

    plan = {
        "env": {
            "contexts": [
                {
                    "name": "Default Context",
                    "urls": [target_url],
                    "includePaths": [f"{target_url}.*"],
                }
            ],
            # True so a genuine internal job error (bad URL, unreachable
            # target) halts the plan -- alerts/findings are never treated as
            # errors by the Automation Framework regardless of this setting.
            "parameters": {"failOnError": True, "progressToStdout": True},
        },
        "jobs": jobs,
    }
    return yaml.safe_dump(plan, sort_keys=False)


# The Automation Framework's own CLI entrypoint (`zap.sh -cmd -autorun`) is
# documented to reflect plan-execution success rather than alert count, but
# this wrapper doesn't rely on that assumption: it explicitly ignores ZAP's
# own exit code (the leading `;`, not `&&`) and instead gates success on
# whether the report file actually got written. A Kubernetes Job's
# initContainers must all exit 0 before the main container starts -- if
# ZAP's exit code leaked alert-count information into that gate, finding
# real vulnerabilities would fail the Job *before the reporter ever runs*,
# silently dropping the one report that had something to say.
_ZAP_ENTRYPOINT = (
    f"zap.sh -cmd -autorun /zap/wrk/plan.yaml; "
    f'echo "zap.sh exited $?"; '
    f"test -f {ZAP_REPORT_DIR}/{ZAP_REPORT_FILENAME}"
)


# Unlike ZAP's wrapper above, this one DOES check the scan's own exit
# code -- verified empirically (real `docker run` against the pinned
# digest, v3.11.1, scanning a live target that actually matched two
# templates) that Nuclei exits 0 whether or not it finds anything, so
# there's no "exit 0 might just mean it found alerts" ambiguity to work
# around. Without this, an initContainer that writes a partial/empty
# report before crashing would still pass `test -f` and succeed, and
# main()'s scan_completed=True default for Nuclei (see ParsedReport's
# docstring in reporter.py) would then treat that partial file as a
# genuinely clean scan -- archiving every real, previously-tracked
# finding on the strength of a crash, not a clean run.
#
# No `-t`/`-update-template-dir` flags -- verified by hand (real
# `docker run` against the pinned digest, v3.11.1) that `-td` was never a
# real flag, and that `-t`/`-update-template-dir` don't compose the way
# pointing both at the same custom directory would suggest: doing that
# caused Nuclei to decide templates "weren't installed" there and
# auto-install its own default set into a second, nested directory, so
# the scan silently ran a different, smaller template set than the one
# `-update-templates` had just written. The simpler, verified-working
# pattern relies on `$HOME` alone (see the container's HOME env var,
# pointed at NUCLEI_TEMPLATES_DIR): both commands land templates/config
# under Nuclei's own default locations beneath it, and the scan step
# finds them with no extra flags needed.
def _nuclei_entrypoint(target_url: str) -> str:
    # Politeness: Nuclei's real defaults (verified via -h against the pinned
    # digest) are -rate-limit 150 (req/s) and -concurrency 25 -- aggressive
    # for a single WAF-protected QA route it's the only thing hitting.
    # Same rationale as ZAP's threadPerHost/delayInMs below: capped well
    # under the defaults so a scheduled scan doesn't read as an attack to
    # whatever's fronting the target. Revisit alongside the route owner
    # before this schedule is enabled in earnest, same as ZAP's `full` mode.
    return (
        f"nuclei -update-templates; "
        f"nuclei -target {target_url} -rate-limit 10 -concurrency 10 "
        f"-jsonl -output {NUCLEI_REPORT_PATH}; "
        f"SCAN_EXIT=$?; "
        f'echo "nuclei scan exited $SCAN_EXIT"; '
        f'test -f {NUCLEI_REPORT_PATH} && [ "$SCAN_EXIT" -eq 0 ]'
    )


def _reporter_container(
    *, tool: str, target_name: str, target_url: str, report_path: str
) -> core.v1.ContainerArgs:
    return core.v1.ContainerArgs(
        name="reporter",
        image=reporter_image,
        env=[
            core.v1.EnvVarArgs(name="VULN_SCANNER_TOOL", value=tool),
            core.v1.EnvVarArgs(name="VULN_SCANNER_TARGET_NAME", value=target_name),
            core.v1.EnvVarArgs(name="VULN_SCANNER_TARGET_URL", value=target_url),
            core.v1.EnvVarArgs(name="VULN_SCANNER_REPORT_PATH", value=report_path),
            core.v1.EnvVarArgs(name="VULN_SCANNER_S3_BUCKET", value=report_bucket_name),
            core.v1.EnvVarArgs(name="AWS_REGION", value=aws_config.region),
        ],
        volume_mounts=[
            core.v1.VolumeMountArgs(name="shared-reports", mount_path="/shared")
        ],
        resources=core.v1.ResourceRequirementsArgs(
            requests={"cpu": "100m", "memory": "256Mi"},
            limits={"cpu": "500m", "memory": "512Mi"},
        ),
        security_context=_hardened_security_context(),
    )


def _cronjob(
    *,
    name: str,
    schedule: str,
    init_containers: list[core.v1.ContainerArgs],
    reporter_container: core.v1.ContainerArgs,
    volumes: list[core.v1.VolumeArgs],
    pod_security_context: core.v1.PodSecurityContextArgs,
    depends_on: list[Resource],
) -> batch.v1.CronJob:
    return batch.v1.CronJob(
        f"vuln-scanner-{name}-{stack_info.env_suffix}",
        metadata=meta.v1.ObjectMetaArgs(
            name=name,
            namespace=vuln_scanner_namespace,
            labels=application_labels,
        ),
        spec=batch.v1.CronJobSpecArgs(
            schedule=schedule,
            concurrency_policy="Forbid",
            starting_deadline_seconds=600,
            successful_jobs_history_limit=3,
            # More failures than successes retained -- a failed scan is the
            # one whose logs somebody needs.
            failed_jobs_history_limit=5,
            job_template=batch.v1.JobTemplateSpecArgs(
                metadata=meta.v1.ObjectMetaArgs(labels=application_labels),
                spec=batch.v1.JobSpecArgs(
                    backoff_limit=1,
                    active_deadline_seconds=5400,
                    template=core.v1.PodTemplateSpecArgs(
                        metadata=meta.v1.ObjectMetaArgs(labels=application_labels),
                        spec=core.v1.PodSpecArgs(
                            restart_policy="Never",
                            service_account_name=vuln_scanner_irsa_service_account_name,
                            security_context=pod_security_context,
                            init_containers=init_containers,
                            containers=[reporter_container],
                            volumes=volumes,
                        ),
                    ),
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=depends_on),
    )


zap_cronjobs = []
nuclei_cronjobs = []

for index, target in enumerate(targets):
    target_name = target["name"]
    target_url = target["url"]
    openapi_schema_url = target.get("openapi_schema_url")

    # ---- ZAP ----
    zap_plan_configmap = core.v1.ConfigMap(
        f"vuln-scanner-zap-plan-{target_name}-{stack_info.env_suffix}",
        metadata=meta.v1.ObjectMetaArgs(
            name=f"zap-plan-{target_name}",
            namespace=vuln_scanner_namespace,
            labels=application_labels,
        ),
        data={
            "plan.yaml": _zap_automation_plan(
                target_name, target_url, openapi_schema_url, zap_scan_type
            )
        },
    )

    zap_init_container = core.v1.ContainerArgs(
        name="zap-scan",
        image=ZAP_IMAGE,
        command=["/bin/sh", "-c", _ZAP_ENTRYPOINT],
        volume_mounts=[
            core.v1.VolumeMountArgs(
                name="zap-plan",
                mount_path="/zap/wrk/plan.yaml",
                sub_path="plan.yaml",
            ),
            core.v1.VolumeMountArgs(name="shared-reports", mount_path=ZAP_REPORT_DIR),
        ],
        resources=core.v1.ResourceRequirementsArgs(
            requests={"cpu": "1", "memory": "2Gi"},
            limits={"cpu": "2", "memory": "4Gi"},
        ),
        security_context=core.v1.SecurityContextArgs(
            run_as_non_root=True,
            # Pinned explicitly, not left to the image default -- see
            # _hardened_security_context's comment on why an unverified
            # upstream image can't be trusted to default to a non-root user.
            run_as_user=1000,
            allow_privilege_escalation=False,
            capabilities=core.v1.CapabilitiesArgs(drop=["ALL"]),
            # Not read-only: ZAP writes its own working state/report under
            # /zap/wrk beyond just the shared report path.
            read_only_root_filesystem=False,
        ),
    )

    zap_cronjobs.append(
        _cronjob(
            name=f"zap-{target_name}",
            # Weekly, staggered by target index so multiple targets don't
            # all fire at once.
            schedule=_stagger_schedule(3, index),
            init_containers=[zap_init_container],
            reporter_container=_reporter_container(
                tool="zap",
                target_name=target_name,
                target_url=target_url,
                report_path=f"/shared/{ZAP_REPORT_FILENAME}",
            ),
            volumes=[
                core.v1.VolumeArgs(
                    name="shared-reports",
                    empty_dir=core.v1.EmptyDirVolumeSourceArgs(),
                ),
                core.v1.VolumeArgs(
                    name="zap-plan",
                    config_map=core.v1.ConfigMapVolumeSourceArgs(
                        name=zap_plan_configmap.metadata.name
                    ),
                ),
            ],
            pod_security_context=core.v1.PodSecurityContextArgs(fs_group=1000),
            depends_on=[zap_plan_configmap, vuln_scanner_auth_binding],
        )
    )

    # ---- Nuclei ----
    nuclei_init_container = core.v1.ContainerArgs(
        name="nuclei-scan",
        image=NUCLEI_IMAGE,
        command=["/bin/sh", "-c", _nuclei_entrypoint(target_url)],
        env=[
            # Verified by hand (real `docker run`, v3.11.1): Nuclei installs
            # templates to $HOME/nuclei-templates and writes its own
            # config/cache to $HOME/.config/nuclei and $HOME/.cache/nuclei --
            # all three land under this one writable volume just by setting
            # HOME, no other flags needed. With read_only_root_filesystem=True
            # and no volume over the image's default $HOME (/root), those
            # writes would otherwise fail or silently no-op every run.
            core.v1.EnvVarArgs(name="HOME", value=NUCLEI_TEMPLATES_DIR),
        ],
        volume_mounts=[
            core.v1.VolumeMountArgs(name="shared-reports", mount_path="/shared"),
            core.v1.VolumeMountArgs(
                name="nuclei-templates", mount_path=NUCLEI_TEMPLATES_DIR
            ),
        ],
        resources=core.v1.ResourceRequirementsArgs(
            requests={"cpu": "500m", "memory": "512Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
        # run_as_user pinned explicitly: ProjectDiscovery's official Nuclei
        # image has historically shipped with no USER directive (defaults
        # to root), which would otherwise fail kubelet's run_as_non_root
        # check outright and silently block every scheduled scan.
        security_context=_hardened_security_context(run_as_user=1000),
    )

    nuclei_cronjobs.append(
        _cronjob(
            name=f"nuclei-{target_name}",
            # Weekly, offset an hour after ZAP so they don't contend for
            # cluster resources.
            schedule=_stagger_schedule(4, index),
            init_containers=[nuclei_init_container],
            reporter_container=_reporter_container(
                tool="nuclei",
                target_name=target_name,
                target_url=target_url,
                report_path=NUCLEI_REPORT_PATH,
            ),
            volumes=[
                core.v1.VolumeArgs(
                    name="shared-reports",
                    empty_dir=core.v1.EmptyDirVolumeSourceArgs(),
                ),
                # The one writable exception to readOnlyRootFilesystem --
                # Nuclei's template feed updates far more often than its
                # image does, so template refresh needs a writable
                # directory even with the image itself pinned by digest.
                # See the plan doc's "Template freshness vs. image
                # pinning" section.
                core.v1.VolumeArgs(
                    name="nuclei-templates",
                    empty_dir=core.v1.EmptyDirVolumeSourceArgs(),
                ),
            ],
            pod_security_context=core.v1.PodSecurityContextArgs(fs_group=1000),
            depends_on=[vuln_scanner_auth_binding],
        )
    )
