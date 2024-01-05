from ol_concourse.lib.constants import REGISTRY_IMAGE
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
from ol_concourse.lib.resource_types import pypi_resource
from ol_concourse.lib.resources import git_repo, pypi

plugin_dict = {
    "src/edx_sysadmin": {
        "target_name": "edx_sysadmin_package",
        "pypi_package_name": "edx-sysadmin",
    },
    "src/ol_openedx_canvas_integration": {
        "target_name": "canvas_integration_package",
        "pypi_package_name": "ol-openedx-canvas-integration",
    },
    "src/ol_openedx_checkout_external": {
        "target_name": "checkout_external_package",
        "pypi_package_name": "ol-openedx-checkout-external",
    },
    "src/ol_openedx_course_export": {
        "target_name": "course_export_package",
        "pypi_package_name": "ol-openedx-course-export",
    },
    "src/ol_openedx_course_structure_api": {
        "target_name": "course_structure_api_package",
        "pypi_package_name": "ol-openedx-course-structure-api",
    },
    "src/ol_openedx_git_auto_export": {
        "target_name": "edx_git_auto_export",
        "pypi_package_name": "ol-openedx-git-auto-export",
    },
    "src/ol_openedx_logging": {
        "target_name": "logging_package",
        "pypi_package_name": "ol-openedx-logging",
    },
    "src/ol_openedx_otel_monitoring": {
        "target_name": "otel_monitoring_package",
        "pypi_package_name": "ol-openedx-otel-monitoring",
    },
    "src/ol_openedx_rapid_response_reports": {
        "target_name": "rapid_response_plugin_dist",
        "pypi_package_name": "ol-openedx-rapid-response-reports",
    },
    "src/ol_openedx_sentry": {
        "target_name": "sentry_package",
        "pypi_package_name": "ol-openedx-sentry",
    },
}

git_resource_list = []
pypi_resource_list = []
job_list = []
for path, config in plugin_dict.items():
    git_resource_list.append(
        git_repo(
            Identifier(f"{config['target_name']}-repo"),
            uri="https://github.com/mitodl/open-edx-plugins",
            paths=[path],
            check_every="24h",
        )
    )
    pypi_resource_list.append(
        pypi(
            Identifier(f"{config['target_name']}-pypi-package"),
            package_name=config["pypi_package_name"],
            username="((pypi_creds.username))",
            password="((pypi_creds.password))",  # noqa: S106
        )
    )
    job_list.append(
        Job(
            name=f"build-{config['target_name']}",
            plan=[
                GetStep(
                    get=git_resource_list[-1].name,
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier(f"pants-package-{config['target_name']}"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type=REGISTRY_IMAGE,
                            source=RegistryImage(repository="python", tag="3.9"),
                        ),
                        inputs=[Input(name=git_resource_list[-1].name)],
                        outputs=[Output(name=git_resource_list[-1].name)],
                        run=Command(
                            path="sh",
                            args=[
                                "-exc",
                                f"""
                                    cd {git_resource_list[-1].name};
                                    ./pants package {path}:{config['target_name']};
                                """,
                            ],
                        ),
                    ),
                ),
                PutStep(
                    put=pypi_resource_list[-1].name,
                    inputs=[
                        git_resource_list[-1].name
                    ],  # This errors when wrapped in Input() like it should be ?
                    params={
                        "glob": f"{git_resource_list[-1].name}/dist/{config['pypi_package_name']}-*.tar.gz",  # noqa: E501
                    },
                ),
            ],
        )
    )

pipeline = Pipeline(
    resource_types=[pypi_resource()],
    resources=git_resource_list + pypi_resource_list,
    jobs=job_list,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-main sp -p publish-open-edx-plugins-pypi -c definition.json")  # noqa: T201
