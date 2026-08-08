"""Scheduled least-privilege drift check for this repo's IAM policy modules.

Each target diffs one Concourse worker role's real AWS API usage (IAM Access
Analyzer: CloudTrail-derived policy generation merged with the unused-access
finding history) against the union of the ``iam_policies`` modules attached to
that role, then opens or updates a pull request proposing the difference.

The point is that the least-privilege policies added in #4873 don't silently
fall out of sync with what the Pulumi stacks actually need -- the failure mode
they replace is an ``AccessDenied`` discovered in production months later.

Prerequisites, both of which have to exist before this pipeline can do anything:

- ``iam_drift_analysis`` attached to the analyzed role, granting the Access
  Analyzer calls the check makes. Wired to the Production ``infra`` pool in
  ``src/ol_infrastructure/applications/concourse/Pulumi.Production.yaml``.
- ``((github.drift_pr_access_token))`` in the ``infrastructure`` team's Vault
  namespace: a token with ``public_repo`` scope on ``mitodl/ol-infrastructure``
  (push a branch, open and update a pull request). The existing tokens in
  ``infrastructure/github`` back read-oriented resources, so this deliberately
  asks for its own rather than assuming one of them can write.
"""

import sys
from dataclasses import dataclass, field

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    Platform,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

AWS_REGION = "us-east-1"
GITHUB_REPOSITORY = "mitodl/ol-infrastructure"
GITHUB_TOKEN_VAULT_PATH = "((github.drift_pr_access_token))"  # noqa: S105
IAM_POLICIES_PACKAGE = "ol_infrastructure.applications.concourse.iam_policies"


@dataclass
class DriftTarget:
    """One role/policy pairing to keep in sync, and where to propose the diff.

    :param name: Job and branch suffix; stable across runs so the same pull
        request is reused rather than a fresh one opened every week.
    :param role_name_prefix: Pulumi auto-names these roles with a random
        suffix, so the check resolves the ARN at run time from this prefix.
    :param policy_modules: Every module attached to the role. Anything missing
        here shows up as spurious "used but not granted" drift.
    :param target_module: The one module out of ``policy_modules`` that
        proposed changes are written to.
    """

    name: str
    role_name_prefix: str
    target_module: str
    policy_modules: list[str] = field(default_factory=list)


# Only the Production infra pool is tracked. It's the only pool that runs
# pulumi_job() steps, so it's where the action set actually moves; the ocw and
# generic pools hold small hand-curated policies that don't drift. Analyzing
# QA/CI as well would be worse than useless -- their roles exercise the same
# policy modules against a fraction of the usage, so they'd propose removing
# actions Production genuinely needs. Adding a target is one entry here plus
# `iam_drift_analysis` on that pool's role.
DRIFT_TARGETS = [
    DriftTarget(
        name="pulumi-infra",
        role_name_prefix="concourse-instance-role-worker-infra-production-",
        target_module=f"{IAM_POLICIES_PACKAGE}.pulumi_infra",
        policy_modules=[
            f"{IAM_POLICIES_PACKAGE}.{module}"
            for module in (
                "base",
                "cloud_custodian",
                "iam_drift_analysis",
                "infra",
                "operations",
                "pulumi_infra",
                "pulumi_state",
            )
        ],
    ),
]

ol_infrastructure = git_repo(
    Identifier("ol-infrastructure"),
    uri=f"https://github.com/{GITHUB_REPOSITORY}",
)

drift_schedule = schedule(
    Identifier("weekly-schedule"),
    days=["Monday"],
    start="08:00",
    stop="09:00",
)

ol_infrastructure_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={
        "repository": dockerhub_ecr_image_uri("mitodl/ol-infrastructure"),
        "tag": "latest",
        "aws_region": ECR_REGION,
    },
)


def drift_job(target: DriftTarget) -> Job:
    """Build the analyze-then-propose job for a single drift target."""
    policy_module_args = " ".join(
        f"--policy-module {module}" for module in target.policy_modules
    )
    return Job(
        name=Identifier(f"check-{target.name}-iam-drift"),
        plan=[
            GetStep(get=drift_schedule.name, trigger=True),
            GetStep(get=ol_infrastructure.name, trigger=False),
            TaskStep(
                task=Identifier(f"propose-{target.name}-iam-policy"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=ol_infrastructure_image,
                    inputs=[Input(name=ol_infrastructure.name)],
                    params={
                        # The checkout, not the image's installed copy: the
                        # policy module the check reads has to be the same one
                        # it edits and pushes.
                        "PYTHONPATH": f"{ol_infrastructure.name}/src",
                        "AWS_DEFAULT_REGION": AWS_REGION,
                        "GITHUB_TOKEN": GITHUB_TOKEN_VAULT_PATH,
                    },
                    run=Command(
                        user="root",
                        path="sh",
                        args=[
                            "-exc",
                            (
                                f"python {ol_infrastructure.name}/bin/"
                                "analyze-pulumi-iam-usage propose"
                                f" {target.role_name_prefix}"
                                f" {policy_module_args}"
                                f" --target-module {target.target_module}"
                                f" --repo-root {ol_infrastructure.name}"
                                " --pr-body-file pr_body.md"
                                "\n"
                                f"python {ol_infrastructure.name}/bin/open-drift-pr"
                                f" --repo-dir {ol_infrastructure.name}"
                                f" --repo {GITHUB_REPOSITORY}"
                                f" --branch iam-drift/{target.name}"
                                " --title 'chore(concourse): sync"
                                f" {target.target_module.rsplit('.', 1)[-1]} IAM"
                                " policy with observed usage'"
                                " --body-file pr_body.md"
                            ),
                        ],
                    ),
                ),
            ),
        ],
    )


def iam_drift_pipeline() -> Pipeline:
    return Pipeline(
        resources=[drift_schedule, ol_infrastructure],
        jobs=[drift_job(target) for target in DRIFT_TARGETS],
    )


if __name__ == "__main__":
    definition_json = iam_drift_pipeline().model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(definition_json)
    sys.stdout.write(definition_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p iam-policy-drift -c definition.json")  # noqa: T201
