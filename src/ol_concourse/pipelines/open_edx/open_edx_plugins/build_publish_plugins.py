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
from ol_concourse.lib.resource_types import pypi_resource
from ol_concourse.lib.resources import git_repo, pypi

pypi_type = pypi_resource()

plugin_dict = {
    "src/edx_sysadmin": {
        "target_name": "edx_sysadmin_package",
        "pypi_package_name": "edx-sysadmin",
    },
    "src/edx_username_changer": {
        "target_name": "edx_username_changer_package",
        "pypi_package_name": "edx-username-changer",
    },
    "src/ol_openedx_canvas_integration": {
        "target_name": "canvas_integration_package",
        "pypi_package_name": "ol-openedx-canvas-integration",
    },
    "src/ol_openedx_chat": {
        "target_name": "ol_openedx_chat_package",
        "pypi_package_name": "ol-openedx-chat",
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
    "src/ol_openedx_course_sync": {
        "target_name": "ol_openedx_course_sync_package",
        "pypi_package_name": "ol-openedx-course-sync",
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
    "src/ol_social_auth": {
        "target_name": "ol_social_auth_package",
        "pypi_package_name": "ol-social-auth",
    },
    "src/openedx_companion_auth": {
        "target_name": "openedx_companion_auth_package",
        "pypi_package_name": "openedx-companion-auth",
    },
    "src/ol_openedx_chat_xblock": {
        "target_name": "ol_openedx_chat_xblock_package",
        "pypi_package_name": "ol-openedx-chat-xblock",
    },
}

fragments = []
for path, config in plugin_dict.items():
    plugin_git_repo = git_repo(
        Identifier(f"{config['target_name']}-repo"),
        uri="https://github.com/mitodl/open-edx-plugins",
        paths=[path],
        check_every="1h",
    )

    plugin_pypi = pypi(
        Identifier(f"{config['target_name']}-pypi-package"),
        package_name=config["pypi_package_name"],
        username="((pypi_creds.username))",
        password="((pypi_creds.password))",  # noqa: S106
    )
    build_job = Job(
        name=f"build-{config['target_name']}",
        plan=[
            GetStep(
                get=plugin_git_repo.name,
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
                    inputs=[Input(name=plugin_git_repo.name)],
                    outputs=[Output(name=plugin_git_repo.name)],
                    run=Command(
                        path="sh",
                        args=[
                            "-exc",
                            f"""
                                cd {plugin_git_repo.name};
                                ./pants package {path}:{config["target_name"]};
                            """,
                        ],
                    ),
                ),
            ),
            PutStep(
                put=plugin_pypi.name,
                inputs=[
                    plugin_git_repo.name
                ],  # This errors when wrapped in Input() like it should be ?
                params={
                    "glob": f"{plugin_git_repo.name}/dist/{config['pypi_package_name']}-*.tar.gz",  # noqa: E501
                },
            ),
        ],
    )
    fragment = PipelineFragment(
        resource_types=[pypi_type],
        resources=[plugin_git_repo, plugin_pypi],
        jobs=[build_job],
    )
    fragments.append(fragment)

combined_fragment = PipelineFragment.combine_fragments(*fragments)
pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources,
    jobs=combined_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-main sp -p publish-open-edx-plugins-pypi -c definition.json")  # noqa: T201
