from ol_concourse.lib.constants import REGISTRY_IMAGE
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
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.notifications import notification
from ol_concourse.lib.resource_types import slack_notification_resource as snr_type
from ol_concourse.lib.resources import schedule, slack_notification, ssh_git_repo

build_schedule = schedule(Identifier("build-schedule"), "1h")

grafana_dashboards = ssh_git_repo(
    Identifier("grizzly-dashboards"),
    uri="git@github.com:mitodl/grafana-dashboards.git",
    private_key="((grizzly.ssh_key))",
)

loki_alert_rules = ssh_git_repo(
    Identifier("loki-alert-rules"),
    uri="git@github.com:mitodl/grafana-alerts.git",
    private_key="((grizzly.ssh_key))",
    paths=[
        "ci/*",
        "loki-rules/*",
    ],
)

slack_notification_resource = slack_notification(
    Identifier("slack-notification"), url="((grizzly.slack_url))"
)

cortex_alert_rules = ssh_git_repo(
    Identifier("cortex-alert-rules"),
    uri="git@github.com:mitodl/grafana-alerts.git",
    private_key="((grizzly.ssh_key))",
    paths=[
        "ci/*",
        "cortex-rules/*",
    ],
)

alertmanager_config = ssh_git_repo(
    Identifier("alertmanager-config"),
    uri="git@github.com:mitodl/grafana-alerts.git",
    private_key="((grizzly.ssh_key))",
    paths=[
        "ci/*",
        "alertmanager.yaml",
    ],
)

grizzly_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="grafana/grizzly", tag="0.2.0-beta3-amd64"),
)

cortextool_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="grafana/cortex-tools", tag="v0.10.7"),
)

git_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="bitnamilegacy/git", tag="2.35.1"),
)

commit_managed_dashboards_job = Job(
    name="commit-managed-dashboards-from-ci",
    build_log_retention={"days": 2},
    plan=[
        GetStep(get=build_schedule.name, trigger=True),
        GetStep(get=grafana_dashboards.name, trigger=True),
        TaskStep(
            task=Identifier("get-managed-dashboards-from-ci"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=grizzly_registry_image,
                inputs=[Input(name=grafana_dashboards.name)],
                outputs=[Output(name="changed_repo")],
                run=Command(
                    path="sh",
                    args=[
                        "-exc",
                        f""" export GRAFANA_TOKEN="((grizzly.ci_token))";
                                export GRAFANA_URL="((grizzly.ci_url))";
                                grr pull -d managed -t 'DashboardFolder/9lJtCes7z';
                                grr pull -d managed -t 'Dashboard/*';
                                cp -Rv {grafana_dashboards.name}/. changed_repo;
                                mkdir -p changed_repo/managed/folders;
                                mkdir -p changed_repo/managed/dashboards/9lJtCes7z;
                                cp -Rv managed/folders/. changed_repo/managed/folders;
                                cp -Rv managed/dashboards/9lJtCes7z/. changed_repo/managed/dashboards/9lJtCes7z; """,  # noqa: E501
                    ],
                ),
            ),
        ),
        TaskStep(
            task=Identifier("commit-managed-dashboards-from-ci"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=git_registry_image,
                inputs=[Input(name="changed_repo")],
                outputs=[Output(name="changed_repo")],
                run=Command(
                    path="bash",
                    args=[
                        "-xc",
                        """  cd changed_repo;
                                git config user.name "concourse";
                                git config user.email "odl-devops@mit.edu";
                                UNTRACKED_FILES=`git ls-files --other --exclude-standard --directory`;
                                git diff --exit-code;
                                if [ $? != 0 ] || [ "$UNTRACKED_FILES" != "" ]; then
                                  git add .;
                                  git commit -m "Automated git sync for grafana-ci";
                                else
                                  echo "Nothing to commit.";
                                fi;""",  # noqa: E501
                    ],
                ),
            ),
        ),
        PutStep(put=grafana_dashboards.name, params={"repository": "changed_repo"}),
    ],
    on_error=notification(
        slack_notification_resource,
        "Grafana Pipeline Error",
        "Error commiting managed dashboards to GitHub",
        alert_type="errored",
    ),
    on_failure=notification(
        slack_notification_resource,
        "Grafana Pipeline Failure",
        "Failure commiting managed dashboards to GitHub",
        alert_type="failed",
    ),
    on_abort=notification(
        slack_notification_resource,
        "Grafana Pipeline Aborted",
        "User Aborted Pipeline during commiting managed dashboards to GitHub",
        alert_type="aborted",
    ),
)

apply_dashboards_to_qa_job = Job(
    name="apply-managed-dashboards-to-qa",
    build_log_retention={"days": 2},
    plan=[
        GetStep(
            get=grafana_dashboards.name,
            trigger=True,
            passed=[commit_managed_dashboards_job.name],
        ),
        TaskStep(
            task=Identifier("commit-managed-dashboards-qa"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=grizzly_registry_image,
                inputs=[Input(name=grafana_dashboards.name)],
                run=Command(
                    path="sh",
                    args=[
                        "-exc",
                        f""" export GRAFANA_TOKEN="((grizzly.qa_token))";  # noqa: WPS237, F541
                              export GRAFANA_URL="((grizzly.qa_url))";
                              grr apply -d {grafana_dashboards.name}/managed -t '*';""",  # noqa: E501
                    ],
                ),
            ),
        ),
    ],
    on_error=notification(
        slack_notification_resource,
        "Grafana Pipeline Error",
        "Error applying managed dashboards to QA",
        alert_type="errored",
    ),
    on_failure=notification(
        slack_notification_resource,
        "Grafana Pipeline Failure",
        "Failure applying managed dashboards to QA",
        alert_type="failed",
    ),
    on_abort=notification(
        slack_notification_resource,
        "Grafana Pipeline Aborted",
        "User Aborted Pipeline during applying managed dashboards to QA",
        alert_type="aborted",
    ),
    on_success=notification(
        slack_notification_resource,
        "Grafana Pipeline Activity",
        "Managed dashboards successfully applied to QA",
        alert_type="success",
    ),
)

apply_dashboards_to_production_job = Job(
    name="apply-managed-dashboards-to-production",
    build_log_retention={"days": 2},
    plan=[
        GetStep(
            get=grafana_dashboards.name,
            trigger=True,
            passed=[apply_dashboards_to_qa_job.name],
        ),
        TaskStep(
            task=Identifier("commit-managed-dashboards-production"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=grizzly_registry_image,
                inputs=[Input(name=grafana_dashboards.name)],
                run=Command(
                    path="sh",
                    args=[
                        "-exc",
                        f""" export GRAFANA_TOKEN="((grizzly.production_token))";  # noqa: WPS237, F541
                              export GRAFANA_URL="((grizzly.production_url))";
                              grr apply -d {grafana_dashboards.name}/managed -t '*';""",  # noqa: E501
                    ],
                ),
            ),
        ),
    ],
    on_error=notification(
        slack_notification_resource,
        "Grafana Pipeline Error",
        "Error applying managed dashboards to Production",
        alert_type="errored",
    ),
    on_failure=notification(
        slack_notification_resource,
        "Grafana Pipeline Failure",
        "Failure applying managed dashboards to Production",
        alert_type="failed",
    ),
    on_abort=notification(
        slack_notification_resource,
        "Grafana Pipeline Aborted",
        "User Aborted Pipeline during applying managed dashboards to Production",
        alert_type="aborted",
    ),
    on_success=notification(
        slack_notification_resource,
        "Grafana Pipeline Activity",
        "Managed dashboards successfully applied to Production",
        alert_type="success",
    ),
)

dashboards_combined_fragment = PipelineFragment(
    resources=[grafana_dashboards, build_schedule],
    jobs=[
        commit_managed_dashboards_job,
        apply_dashboards_to_qa_job,
        apply_dashboards_to_production_job,
    ],
)

alerting_jobs = []
for tool in ["loki", "cortex", "alertmanager"]:
    # Add a linter step for loki and cortex configurations
    if tool in ["loki", "cortex"]:
        resource_name = f"{tool}-alert-rules"
        linter_job = Job(
            name=f"lint-managed-{tool}-rules",
            plan=[
                GetStep(
                    get=resource_name,
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier("lint-rules"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=cortextool_registry_image,
                        inputs=[Input(name=resource_name)],
                        outputs=[Output(name=resource_name)],
                        run=Command(
                            path="sh",
                            args=[
                                "-exc",
                                (
                                    "cortextool rules lint"
                                    f" --backend={tool} {resource_name}/{tool}-rules/*.yaml;"  # noqa: E501
                                ),
                            ],
                        ),
                    ),
                ),
                TaskStep(
                    task=Identifier("commit-linted-rules"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=git_registry_image,
                        inputs=[Input(name=resource_name)],
                        outputs=[Output(name=resource_name)],
                        run=Command(
                            path="bash",
                            args=[
                                "-xc",
                                f"""cd {resource_name};
                                    git config user.name "concourse";
                                    git config user.email "odl-devops@mit.edu";
                                    UNTRACKED_FILES=`git ls-files --other --exclude-standard --directory`;
                                    git diff --exit-code;
                                    if [ $? != 0 ] || [ "$UNTRACKED_FILES" != "" ]; then
                                      git add .;
                                      git commit -m "Automated linter commit";
                                    else
                                      echo "Nothing to commit.";
                                    fi
                                """,  # noqa: E501
                            ],
                        ),
                    ),
                ),
            ],
            on_error=notification(
                slack_notification_resource,
                "Grafana Pipeline Error",
                f"Error linting {tool} configs",
                alert_type="errored",
            ),
            on_failure=notification(
                slack_notification_resource,
                "Grafana Pipeline Failure",
                f"Failure linting {tool} configs",
                alert_type="failed",
            ),
            on_abort=notification(
                slack_notification_resource,
                "Grafana Pipeline Aborted",
                f"User Aborted Pipeline during linting {tool} configs",
                alert_type="aborted",
            ),
        )
        alerting_jobs.append(linter_job)
    else:
        resource_name = f"{tool}-config"
    for stage in ["ci", "qa", "production"]:
        params = {
            "CORTEX_API_KEY": f"((cortextool.cortex-api-key-{stage}))",
            "CORTEX_API_USER": f"((cortextool.{tool}-rules-api-user-{stage}))",
            "CORTEX_TENANT_ID": f"((cortextool.{tool}-rules-api-user-{stage}))",
            "SLACK_NOTIFICATIONS_OCW_MISC_API_URL": (
                "((cortextool.slack-notifications-ocw-misc-api-url))"
            ),
            "ROOTLY_BEARER_TOKEN": f"((cortextool.rootly-bearer-token-{stage}))",
            "ENVIRONMENT_NAME": stage.upper(),
            "RESOURCE_NAME": resource_name,
        }
        if tool in ["cortex", "loki"]:
            job_name = f"sync-managed-{tool}-rules-{stage}"
            interpolate_command = "$RESOURCE_NAME/ci/interpolate_rules_yaml.sh"
            command = (
                "cortextool rules sync $RESOURCE_NAME/$RULE_DIRECTORY/*"
                if tool == "cortex"
                else "cortextool rules sync --backend=loki $RESOURCE_NAME/$RULE_DIRECTORY/*"  # noqa: E501
            )
            directory = f"{tool}-rules"
            params["CORTEX_ADDRESS"] = f"((cortextool.{tool}-rules-api-address))"
        else:
            job_name = f"sync-managed-{tool}-config-{stage}"
            interpolate_command = "$RESOURCE_NAME/ci/interpolate_alertmanager_yaml.sh"
            command = "cortextool alertmanager load $RESOURCE_NAME/alertmanager.yaml"
            directory = ""
            params["CORTEX_ADDRESS"] = "((cortextool.cortex-amconfig-api-address))"
            params["CORTEX_API_USER"] = (
                f"((cortextool.cortex-amconfig-api-user-{stage}))"
            )
            params["CORTEX_TENANT_ID"] = (
                f"((cortextool.cortex-amconfig-api-user-{stage}))"
            )
            params["ENVIRONMENT_NAME"] = stage
        params["RULE_DIRECTORY"] = directory
        passed_value = [alerting_jobs[-1].name]
        if tool == "alertmanager" and stage == "ci":
            passed_value = []
        sync_job = Job(
            name=job_name,
            plan=[
                GetStep(get=resource_name, trigger=True, passed=passed_value),
                TaskStep(
                    task=Identifier("push-to-grafana"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=cortextool_registry_image,
                        inputs=[Input(name=resource_name)],
                        params=params,
                        run=Command(
                            path="sh",
                            args=[
                                "-exc",
                                f"""{interpolate_command};
                                    {command};""",
                            ],
                        ),
                    ),
                ),
            ],
            on_error=notification(
                slack_notification_resource,
                "Grafana Pipeline Error",
                f"Error syncing {tool} configs in {stage}",
                alert_type="errored",
            ),
            on_failure=notification(
                slack_notification_resource,
                "Grafana Pipeline Failure",
                f"Failure syncing {tool} configs {stage}",
                alert_type="failed",
            ),
            on_abort=notification(
                slack_notification_resource,
                "Grafana Pipeline Aborted",
                f"User Aborted Pipeline during syncing {tool} configs in {stage}",
                alert_type="aborted",
            ),
            on_success=notification(
                slack_notification_resource,
                "Grafana Pipeline Activity",
                f"Sync complete for {tool} configs to {stage}.",
                alert_type="success",
            ),
        )
        alerting_jobs.append(sync_job)

alerting_combined_fragment = PipelineFragment(
    resource_types=[snr_type()],
    resources=[
        cortex_alert_rules,
        loki_alert_rules,
        alertmanager_config,
        slack_notification_resource,
    ],
    jobs=alerting_jobs,
)

fully_combined_fragment = PipelineFragment(
    resource_types=dashboards_combined_fragment.resource_types
    + alerting_combined_fragment.resource_types,
    resources=dashboards_combined_fragment.resources
    + alerting_combined_fragment.resources,
    jobs=dashboards_combined_fragment.jobs + alerting_combined_fragment.jobs,
)

grafana_pipeline = Pipeline(
    resource_types=fully_combined_fragment.resource_types,
    resources=fully_combined_fragment.resources,
    jobs=fully_combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(grafana_pipeline.model_dump_json(indent=2))
    sys.stdout.write(grafana_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p misc-grafana-management -c definition.json"
    )  # noqa: RUF100, T201
