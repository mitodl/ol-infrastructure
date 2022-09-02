from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import (
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
from concourse.lib.resources import schedule, ssh_git_repo

build_schedule = schedule(Identifier("build-schedule"), "1h")

grafana_dashboards = ssh_git_repo(
    Identifier("grizzly-dashboards"),
    uri="git@github.com:mitodl/grafana-dashboards.git",
    private_key="((grizzly.ssh_key))",
)

grizzly_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="grafana/grizzly", tag="0.2.0-beta3-amd64"),
)

git_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE, source=RegistryImage(repository="bitnami/git", tag="2.35.1")
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
                                cp -Rv managed/dashboards/9lJtCes7z/. changed_repo/managed/dashboards/9lJtCes7z; """,
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
                        "-exc",
                        """  cd changed_repo;
                                git config user.name "concourse";
                                git config user.email "odl-devops@mit.edu":
                                UNTRACKED_FILES=`git ls-files --other --exclude-standard --directory`;
                                git diff --exit-code;
                                if [ $? != 0 ] || [ "$UNTRACKED_FILES" != "" ]; then
                                  git add .;
                                  git commit -m "Automated git sync for grafana-ci";
                                else
                                  echo "Nothing to commit.";
                                fi;""",
                    ],
                ),
            ),
        ),
        PutStep(
            put=grafana_dashboards.name, params={"repository": grafana_dashboards.name}
        ),
    ],
)

apply_to_qa_job = Job(
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
                        f""" export GRAFANA_TOKEN="((grizzly.qa_token))";
                              export GRAFANA_URL="((grizzly.qa_url))";
                              grr apply -d {{grafana_dashboards.name}}/managed -t '*';""",
                    ],
                ),
            ),
        ),
    ],
)

apply_to_production_job = Job(
    name="apply-managed-dashboards-to-production",
    build_log_retention={"days": 2},
    plan=[
        GetStep(
            get=grafana_dashboards.name, trigger=True, passed=[apply_to_qa_job.name]
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
                        f""" export GRAFANA_TOKEN="((grizzly.production_token))";
                              export GRAFANA_URL="((grizzly.production_url))";
                              grr apply -d {{grafana_dashboards.name}}/managed -t '*';""",
                    ],
                ),
            ),
        ),
    ],
)

combined_fragment = PipelineFragment(
    resources=[grafana_dashboards, build_schedule],
    jobs=[commit_managed_dashboards_job, apply_to_qa_job, apply_to_production_job],
)


grafana_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources,
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "wt") as definition:
        definition.write(grafana_pipeline.json(indent=2))
    sys.stdout.write(grafana_pipeline.json(indent=2))
