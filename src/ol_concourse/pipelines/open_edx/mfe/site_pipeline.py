"""Concourse pipeline for building and deploying OEP-65 Site Projects.

Each Site Project is a single frontend-base build covering all registered MFE
apps for one deployment.  Unlike the legacy per-MFE pipeline, the artifact is
environment-agnostic: built once in CI using lehrer's Dagger module, then
promoted unchanged to QA and Production via rclone cross-bucket copy.

Usage (called by meta.py, analogous to pipeline.py):

    python site_pipeline.py <deployment_name>

    e.g. python site_pipeline.py mitxonline

Supported deployments: mitxonline, mitx, xpro.

Build: mitodl/dcind:latest (Docker-in-Docker) + ``dagger call mfe build-site``.
Promote: rclone cross-bucket copy — no rebuild at QA or Production.
IAM: Concourse worker role has cross-deployment S3 access for all three buckets.
"""

import sys
import textwrap
from dataclasses import dataclass, field
from typing import Any

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import github_issues_resource, rclone
from ol_concourse.lib.resources import git_repo, github_issues

from bridge.settings.github.team_members import DEVOPS_MIT
from ol_concourse.pipelines.constants import GH_ISSUES_DEFAULT_REPOSITORY

LEHRER_URI = "https://github.com/mitodl/lehrer"


@dataclass
class SiteProjectEnv:
    deployment_name: str
    environment: str  # e.g. "mitxonline-ci"
    environment_stage: str  # "CI", "QA", "Production"


@dataclass
class SiteProjectConfig:
    """Configuration for one OEP-65 Site Project across its promotion chain."""

    deployment_name: str
    envs: list[SiteProjectEnv] = field(
        default_factory=list
    )  # ordered CI → QA → Production


SITE_PROJECTS: list[SiteProjectConfig] = [
    SiteProjectConfig(
        deployment_name="mitxonline",
        envs=[
            SiteProjectEnv("mitxonline", "mitxonline-ci", "CI"),
            SiteProjectEnv("mitxonline", "mitxonline-qa", "QA"),
            SiteProjectEnv("mitxonline", "mitxonline-production", "Production"),
        ],
    ),
    SiteProjectConfig(
        deployment_name="mitx",
        envs=[
            SiteProjectEnv("mitx", "mitx-ci", "CI"),
            SiteProjectEnv("mitx", "mitx-qa", "QA"),
            SiteProjectEnv("mitx", "mitx-production", "Production"),
        ],
    ),
    SiteProjectConfig(
        deployment_name="xpro",
        envs=[
            SiteProjectEnv("xpro", "xpro-ci", "CI"),
            SiteProjectEnv("xpro", "xpro-qa", "QA"),
            SiteProjectEnv("xpro", "xpro-production", "Production"),
        ],
    ),
]


def _lehrer_resource(deployment_name: str) -> Resource:
    return git_repo(
        name=Identifier(f"lehrer-{deployment_name}"),
        uri=LEHRER_URI,
        branch="main",
        paths=[
            f"deployments/mit-ol/mfe_slot_config/frontend/{deployment_name}/",
            "deployments/mit-ol/mfe_slot_config/frontend/shared/",
        ],
    )


def _gh_issues_post(deployment_name: str, stage: str) -> Resource:
    stage_lower = stage.lower()
    title = f"[bot] Site Project {deployment_name} deployed to {stage}"
    return github_issues(
        auth_method="token",
        name=Identifier(f"gh-issues-site-{deployment_name}-{stage_lower}-post"),
        repository=GH_ISSUES_DEFAULT_REPOSITORY,
        issue_title_template=title,
        issue_prefix=title,
        issue_state="open",
    )


def _gh_issues_trigger(deployment_name: str, stage: str) -> Resource:
    """Closed-issue resource that gates promotion from ``stage`` to the next stage."""
    stage_lower = stage.lower()
    title = f"[bot] Site Project {deployment_name} deployed to {stage}"
    return github_issues(
        auth_method="token",
        name=Identifier(f"gh-issues-site-{deployment_name}-{stage_lower}-trigger"),
        repository=GH_ISSUES_DEFAULT_REPOSITORY,
        issue_title_template=title,
        issue_prefix=title,
        issue_state="closed",
    )


def _gh_labels(stage: str) -> list[str]:
    labels = ["product:infrastructure", "DevOps", "pipeline-workflow"]
    stage_lower = stage.lower()
    if stage_lower == "ci":
        labels.append("promotion-to-qa")
    elif stage_lower == "qa":
        labels.append("promotion-to-production")
    else:
        labels.append("finalized-deployment")
    return labels


def site_build_job(
    env: SiteProjectEnv,
    previous_job: Job | None = None,
    trigger_resource: Identifier | None = None,
) -> PipelineFragment:
    """Generate the CI build job for a Site Project.

    Runs ``dagger call mfe build-site`` inside mitodl/dcind:latest (privileged
    DinD), exports dist/, then syncs it to the deployment's CI S3 bucket.
    QA and Production artifacts are promoted via :func:`site_promote_job`.
    """
    lehrer = _lehrer_resource(env.deployment_name)
    post_resource = _gh_issues_post(env.deployment_name, env.environment_stage)

    get_lehrer = GetStep(get=lehrer.name, trigger=True)
    if previous_job:
        get_lehrer.passed = [previous_job.name]

    plan: list[Any] = [get_lehrer]
    if trigger_resource:
        plan.append(GetStep(get=trigger_resource, trigger=True))

    deployment = env.deployment_name
    lehrer_input = str(lehrer.name)
    site_project_path = f"./deployments/mit-ol/mfe_slot_config/frontend/{deployment}"
    shared_src_path = "./deployments/mit-ol/mfe_slot_config/frontend/shared"
    public_path = f"/apps/{deployment}-site/"

    plan.append(
        TaskStep(
            attempts=3,
            task=Identifier(f"build-{deployment}-site"),
            privileged=True,
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={
                        "repository": "mitodl/dcind",
                        "tag": "latest",
                    },
                ),
                inputs=[Input(name=lehrer.name)],
                outputs=[Output(name=Identifier("site-dist"))],
                run=Command(
                    path="bash",
                    args=[
                        "-c",
                        textwrap.dedent(
                            f"""\
                            source /docker-lib.sh
                            start_docker
                            cd {lehrer_input}
                            dagger call mfe build-site \\
                              --site-project {site_project_path} \\
                              --shared-src   {shared_src_path} \\
                              --public-path  {public_path} \\
                              export --path ../site-dist/
                            """
                        ),
                    ],
                ),
            ),
        )
    )

    plan.append(
        PutStep(
            put="mfe-site-bucket",
            params={
                "source": "site-dist",
                "destination": [
                    {
                        "command": "sync",
                        "dir": (
                            f"s3-remote:{env.environment}-edxapp-mfe/{deployment}-site/"
                        ),
                    }
                ],
            },
        )
    )

    job = Job(
        name=Identifier(f"build-{deployment}-site-{env.environment}"),
        plan=plan,
        on_success=PutStep(
            put=post_resource.name,
            params={
                "labels": _gh_labels(env.environment_stage),
                "assignees": DEVOPS_MIT,
            },
        ),
    )

    return PipelineFragment(resources=[lehrer, post_resource], jobs=[job])


def site_promote_job(
    env: SiteProjectEnv,
    source_env: SiteProjectEnv,
    trigger_resource: Identifier,
) -> PipelineFragment:
    """Generate a QA or Production promotion job.

    Copies the Site Project artifact from the source S3 bucket to the target
    bucket via rclone — no rebuild required since the artifact is environment-agnostic.
    """
    deployment = env.deployment_name
    post_resource = _gh_issues_post(deployment, env.environment_stage)

    source_bucket = f"{source_env.environment}-edxapp-mfe/{deployment}-site/"
    target_bucket = f"{env.environment}-edxapp-mfe/{deployment}-site/"

    plan: list[Any] = [
        GetStep(
            get=trigger_resource,
            trigger=True,
        ),
        TaskStep(
            attempts=3,
            task=Identifier(f"promote-{deployment}-site-to-{env.environment}"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={"repository": "rclone/rclone", "tag": "latest"},
                ),
                inputs=[],
                params={
                    "RCLONE_CONFIG_S3REMOTE_TYPE": "s3",
                    "RCLONE_CONFIG_S3REMOTE_PROVIDER": "AWS",
                    "RCLONE_CONFIG_S3REMOTE_ENV_AUTH": "true",
                    "RCLONE_CONFIG_S3REMOTE_REGION": "us-east-1",
                },
                run=Command(
                    path="rclone",
                    args=[
                        "copy",
                        f"s3remote:{source_bucket}",
                        f"s3remote:{target_bucket}",
                        "--checksum",
                    ],
                ),
            ),
        ),
    ]

    job = Job(
        name=Identifier(
            f"promote-{deployment}-site-{source_env.environment}-to-{env.environment}"
        ),
        plan=plan,
        on_success=PutStep(
            put=post_resource.name,
            params={
                "labels": _gh_labels(env.environment_stage),
                "assignees": DEVOPS_MIT,
            },
        ),
    )

    return PipelineFragment(resources=[post_resource], jobs=[job])


def site_pipeline(deployment_name: str) -> Pipeline:
    """Generate the OEP-65 Site Project pipeline for one deployment.

    Produces three jobs: CI (build + upload via Dagger), QA (rclone promote),
    Production (rclone promote).  Called by meta.py analogously to pipeline.py.
    """
    _by_name = {p.deployment_name: p for p in SITE_PROJECTS}
    project = _by_name.get(deployment_name)
    if project is None:
        supported = ", ".join(_by_name)
        msg = (
            f"Unknown deployment {deployment_name!r}. "
            f"Supported deployments: {supported}"
        )
        raise SystemExit(msg)
    ci_env, qa_env, prod_env = project.envs

    ci_fragment = site_build_job(ci_env)

    ci_trigger = _gh_issues_trigger(deployment_name, ci_env.environment_stage)
    if ci_trigger.name is None:
        msg = f"ci_trigger resource has no name for {deployment_name}"
        raise RuntimeError(msg)

    qa_fragment = site_promote_job(
        env=qa_env,
        source_env=ci_env,
        trigger_resource=ci_trigger.name,
    )

    qa_trigger = _gh_issues_trigger(deployment_name, qa_env.environment_stage)
    if qa_trigger.name is None:
        msg = f"qa_trigger resource has no name for {deployment_name}"
        raise RuntimeError(msg)

    prod_fragment = site_promote_job(
        env=prod_env,
        source_env=qa_env,
        trigger_resource=qa_trigger.name,
    )

    combined = PipelineFragment.combine_fragments(
        ci_fragment,
        qa_fragment,
        prod_fragment,
        PipelineFragment(resources=[ci_trigger, qa_trigger], jobs=[]),
    )

    return Pipeline(
        resource_types=[
            rclone(),
            github_issues_resource(),
            *combined.resource_types,
        ],
        resources=[
            Resource(
                name=Identifier("mfe-site-bucket"),
                type="rclone",
                source={
                    "config": textwrap.dedent(
                        """\
                        [s3-remote]
                        type = s3
                        provider = AWS
                        env_auth = true
                        region = us-east-1
                        """
                    )
                },
            ),
            *combined.resources,
        ],
        jobs=combined.jobs,
    )


if __name__ == "__main__":
    deployment: str = sys.argv[1]
    pipeline = site_pipeline(deployment)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
