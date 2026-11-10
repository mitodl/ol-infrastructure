"""Non-blocking end-to-end tests for an edxapp stage.

After a stage's deploy job, and once a night, run `openedx/end-to-end-tests`
against that stage. Nothing lists the job in `passed`, so a red run holds up no
promotion: it shows what broke for someone to look at, and Production's preview
and deploy keep waiting on the stage's deploy job alone.
"""

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    InParallelStep,
    Job,
    Platform,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import schedule
from pydantic import BaseModel

from ol_concourse.pipelines.canaries.pipeline import PLAYWRIGHT_IMAGE_REPOSITORY

E2E_SUITE_URI = "https://github.com/openedx/end-to-end-tests"

# The suite commit that runs. Pinned rather than tracking `main`: the suite runs
# with the stage's admin login on infrastructure-team workers, so a new suite
# commit (or a dependency in its lockfile) should be read before it runs here.
# Bump it together with E2E_PLAYWRIGHT_IMAGE_TAG, to the image matching that
# commit's @playwright/test in package-lock.json.
E2E_SUITE_REF = "08e1a7192b2045eb4f496364d443669c5bb8d2a4"  # pragma: allowlist secret

# The suite pins @playwright/test in its own repository, so this tag cannot be
# derived from a package.json here the way the canaries derive theirs. The run
# calls `npx playwright install chromium` after `npm ci`: a no-op while the
# suite's Playwright matches this image, and a browser download -- rather than
# "browserType.launch: Executable doesn't exist" -- if the two ever differ.
E2E_PLAYWRIGHT_IMAGE_TAG = "v1.64.1-noble"


class E2ETarget(BaseModel):
    """One edxapp stage the suite runs against.

    Attributes:
        deployment_name: The edxapp deployment, e.g. ``mitxonline``.
        stage: The stage whose deploy job triggers a run, e.g. ``QA``.
        settings: The suite's non-secret configuration (its ``.env`` keys:
            base URLs, MFE mounts, ``CAPABILITIES``, ...), passed to the run as
            environment variables. Public source: never a credential.
        admin_credential: Name of the Concourse credential holding an LMS
            superuser that has an LMS password, with ``username`` and
            ``password`` keys. Surfaced to the suite as ``ADMIN_USERNAME`` and
            ``ADMIN_PASSWORD``.
        grep_invert: Tags to leave out, as ``--grep-invert`` takes them: surfaces
            the stage does not deploy at all, rather than ones that fail.
        workers: Playwright workers. The stage is shared, so kept low.
        retries: Retries per failing test, so a one-off flake does not turn the
            run red.
        timeout: Budget for the whole run.
        nightly_start: Start of the nightly window, ``HH:MM`` UTC.
        nightly_stop: End of the nightly window, ``HH:MM`` UTC.
    """

    deployment_name: str
    stage: str
    settings: dict[str, str]
    admin_credential: str
    grep_invert: str | None = None
    workers: int = 2
    retries: int = 1
    timeout: str = "4h"
    nightly_start: str = "06:00"
    nightly_stop: str = "07:00"

    @property
    def job_name(self) -> str:
        """The job's name, e.g. ``e2e-tests-mitxonline-qa``."""
        return f"e2e-tests-{self.deployment_name}-{self.stage.lower()}"


E2E_TARGETS: list[E2ETarget] = [
    E2ETarget(
        deployment_name="mitxonline",
        stage="QA",
        settings={
            "LMS_BASE_URL": "https://courses.rc.learn.mit.edu",
            "APPS_BASE_URL": "https://courses.rc.learn.mit.edu",
            "CMS_BASE_URL": "https://studio.courses.rc.learn.mit.edu",
            # Learners register, and the admin signs in, through the LMS
            # registration and login APIs, which mitxonline's
            # MITX_REDIRECT_ALLOW_RE_LIST leaves open (`api`, `user_api`). No
            # Keycloak access needed.
            "ACCOUNT_BACKEND": "automatic",
            # No authn MFE (AUTHN_MICROFRONTEND_URL is unset): sign-in goes
            # through the ol-oauth2 SSO backend. Special exams are on for every
            # deployment (config_builder.py's ENABLE_SPECIAL_EXAMS). lehrer's
            # mitxonline image installs the SCORM and SGA XBlocks, and the
            # courseware navigation sidebar ships with edx-platform master.
            # Capabilities left undeclared make their specs skip, not fail.
            "CAPABILITIES": (
                "-mfe-authn,studio,teams,notes,ora,cohorts,special-exams,"
                "scorm,edx-sga,courseware-navigation-sidebar"
            ),
            "LEARNING_MFE_URL": "https://courses.rc.learn.mit.edu/learn",
            "AUTHORING_MFE_URL": "https://studio.courses.rc.learn.mit.edu/authoring",
            "INSTRUCTOR_DASHBOARD_MFE_URL": (
                "https://courses.rc.learn.mit.edu/apps/instructor-dashboard"
            ),
            "DISCUSSIONS_MFE_URL": "https://courses.rc.learn.mit.edu/discuss",
            # No learner dashboard MFE: the notifications tray is reached from
            # the course home.
            "NOTIFICATION_TRAY_HOST": "course-home",
        },
        # The Concourse credential's NAME, not a credential. Resolves from Vault
        # at secret-concourse/infrastructure/e2e_mitxonline_rc for pr-inf.
        admin_credential="e2e_mitxonline_rc",  # pragma: allowlist secret
        # MFEs mitxonline does not deploy: account (ACCOUNT_MICROFRONTEND_URL is
        # None), learner dashboard (no LEARNER_HOME_MICROFRONTEND_URL), catalog
        # and profile (neither is in Pulumi.mitxonline.QA.yaml's enabled_mfes).
        # These are tags rather than capabilities, so CAPABILITIES cannot turn
        # them off.
        grep_invert="@mfe-account|@mfe-learner-dashboard|@mfe-catalog|@mfe-profile",
    ),
]


def e2e_target(deployment_name: str, stage: str) -> E2ETarget | None:
    """Return the target for a deployment's stage, if it runs the suite."""
    return next(
        (
            target
            for target in E2E_TARGETS
            if target.deployment_name == deployment_name
            and target.stage.lower() == stage.lower()
        ),
        None,
    )


def _run_script(target: E2ETarget) -> str:
    """Build the task script: wait for the stage, then run the pinned suite."""
    test_args = [f"--retries={target.retries}"]
    if target.grep_invert:
        test_args.append(f'--grep-invert "{target.grep_invert}"')
    return "\n".join(
        [
            "set -euo pipefail",
            # The deploy job returns once Pulumi has seen the rollout through,
            # but a nightly run can land during an outage: give both apps up to
            # 10 minutes to answer before reporting every test as failed.
            'for url in "$LMS_BASE_URL/heartbeat" "$CMS_BASE_URL/heartbeat"; do',
            "  curl -fsS -o /dev/null --retry 40 --retry-delay 15"
            ' --retry-all-errors "$url"',
            "done",
            f"git clone --quiet {E2E_SUITE_URI} suite",
            "cd suite",
            f"git checkout --quiet {E2E_SUITE_REF}",
            # The image does not ship @playwright/test, so npm ci is mandatory.
            "npm ci",
            # A no-op while the suite's Playwright matches the image; see
            # E2E_PLAYWRIGHT_IMAGE_TAG.
            "npx playwright install chromium",
            # The results are this build's log. Reports and traces stay in the
            # container: a trace records the admin sign-in's form fields and
            # session cookies, so none of it is uploaded anywhere.
            f"npx playwright test {' '.join(test_args)}",
        ]
    )


def e2e_fragment(
    target: E2ETarget,
    *,
    deploy_job: str,
    image: Resource,
    pulumi_code: Resource,
) -> PipelineFragment:
    """Build the job that runs the suite after ``deploy_job``, and nightly.

    :param target: The stage to test.
    :param deploy_job: The stage's deploy job. A run tests what it deployed.
    :param image: The edxapp image resource the deploy job takes.
    :param pulumi_code: The Pulumi code resource the deploy job takes, so a
        configuration-only deploy is tested too.
    :returns: The job, with the resources it adds.
    """
    nightly = schedule(
        name=Identifier(f"e2e-nightly-{target.deployment_name}-{target.stage.lower()}"),
        interval="24h",
        start=target.nightly_start,
        stop=target.nightly_stop,
    )

    task_params = {
        **target.settings,
        "WORKERS": str(target.workers),
        # Resolved by Concourse's Vault credential manager at task start, so the
        # values never appear in the pipeline definition.
        "ADMIN_USERNAME": f"(({target.admin_credential}.username))",
        "ADMIN_PASSWORD": f"(({target.admin_credential}.password))",
    }

    job = Job(
        name=target.job_name,
        build_log_retention={"builds": 30},
        # A nightly run and a deploy-triggered one would share the stage's
        # courses, users and site-wide switches, and their failures interleave.
        max_in_flight=1,
        plan=[
            InParallelStep(
                in_parallel=[
                    GetStep(
                        get=image.name,
                        trigger=True,
                        passed=[deploy_job],
                        params={"skip_download": True},
                    ),
                    GetStep(get=pulumi_code.name, trigger=True, passed=[deploy_job]),
                    GetStep(get=nightly.name, trigger=True),
                ]
            ),
            TaskStep(
                task=Identifier("run-end-to-end-tests"),
                timeout=target.timeout,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": PLAYWRIGHT_IMAGE_REPOSITORY,
                            "tag": E2E_PLAYWRIGHT_IMAGE_TAG,
                        },
                    ),
                    params=task_params,
                    run=Command(path="bash", args=["-c", _run_script(target)]),
                ),
            ),
        ],
    )

    return PipelineFragment(resources=[nightly], jobs=[job])
