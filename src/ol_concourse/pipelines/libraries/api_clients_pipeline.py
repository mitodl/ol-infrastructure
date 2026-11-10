# src/ol_concourse/pipelines/libraries/api_clients_pipeline.py
import argparse
import sys
from pathlib import Path

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    PutStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image, ssh_git_repo

from bridge.lib.versions import OPENAPI_GENERATOR_VERSION
from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.libraries.configuration import PIPELINE_CONFIGS
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data


def _read_script(script_name: str) -> str:
    """Read a script file from the scripts directory."""
    return (Path(__file__).parent / "scripts" / script_name).read_text()


def _generate_apis_step(
    image: Identifier, source_input: Input, api_clients_repo: Identifier
) -> TaskStep:
    """Run the clients repo's generator over every config/*.yaml.

    The configs name the spec as ``<source_repo_name>/openapi/specs/...``
    relative to the build directory, so *source_input* must be mounted at that
    directory name whatever its resource is called.
    """
    return TaskStep(
        task=Identifier("generate-apis"),
        image=image,
        config=TaskConfig(
            platform="linux",
            inputs=[source_input, Input(name=api_clients_repo)],
            outputs=[Output(name=api_clients_repo)],
            run=Command(
                path="/bin/bash",
                args=[f"{api_clients_repo}/scripts/generate-inner.sh"],
            ),
        ),
    )


def generate_api_client_pipeline(  # noqa: PLR0913
    source_repo_name: str,
    source_repo_uri: str,
    source_repo_branch: str,
    client_repo_name: str,
    client_repo_uri: str,
    client_repo_branch: str,
    client_repo_subpath: str | list[str],
    source_repo_tag_regex: str | None = None,
    prerelease_branch: str | None = None,
) -> Pipeline:
    """
    Generate a pipeline definition for building and publishing API clients.

    :param source_repo_name: The identifier for the source code repository resource.
    :param source_repo_uri: The URI of the source code repository (e.g., GitHub URL).
    :param source_repo_branch: The branch of the source code repository to track.
        Ignored when *source_repo_tag_regex* is set.
    :param client_repo_name: The identifier for the generated client code repository
        resource.
    :param client_repo_uri: The URI of the client code repository (e.g., GitHub SSH
        URL).
    :param client_repo_branch: The branch of the client code repository to push to.
    :param client_repo_subpath: The subpath within the client repo where the generated
        code resides (relative to src/typescript/). Pass a list when the source repo
        publishes several specs and each is packaged separately -- one npm package is
        published per entry, all sharing the repo-root VERSION.
    :param source_repo_tag_regex: Regenerate on tags matching this regex rather than
        on spec changes landing on a branch. For a repo that cuts releases as tags
        and has no long-lived release branch. Note the tradeoff: the git resource
        consults ``paths`` only when versioning on commits, so under a tag regex
        every matching tag rebuilds the client whether or not a spec actually
        moved -- the "only when the interface changed" property comes from the
        release cadence instead. ``branch`` stops applying too, so a matching tag
        anywhere in the repo triggers it.
    :param prerelease_branch: Also publish a prerelease of each package under the
        npm ``next`` dist-tag whenever a spec change lands on this branch of the
        source repo, so a consumer can pin a client for an API that has merged
        but not yet been released. The version
        (``<CalVer date>.0-dev.<unix time>.g<sha7>``) exists only on npm: nothing
        is committed to the client repo, and ``latest`` stays on the stable
        channel above.

    :return: A Pipeline object representing the Concourse pipeline definition.
    """
    client_repo_subpaths = (
        [client_repo_subpath]
        if isinstance(client_repo_subpath, str)
        else client_repo_subpath
    )

    # Define parameterized image tags
    python_image_tag = "3.12-slim"
    node_image_tag = "24-slim"
    openapi_generator_tag = OPENAPI_GENERATOR_VERSION

    # Define script names
    bump_script: str = "api-clients-bumpver.sh"
    commit_script: str = "api-clients-commit-changes.sh"
    publish_script: str = "api-clients-publish-node.sh"
    publish_prerelease_script: str = "api-clients-publish-node-prerelease.sh"

    openapi_generator_image = registry_image(
        name=Identifier("openapi-generator-image"),
        image_repository=dockerhub_ecr_image_uri("openapitools/openapi-generator-cli"),
        image_tag=openapi_generator_tag,
        ecr_region=ECR_REGION,
    )
    python_image = registry_image(
        name=Identifier("python-image"),
        image_repository=dockerhub_ecr_image_uri("python"),
        image_tag=python_image_tag,
        ecr_region=ECR_REGION,
    )
    node_image = registry_image(
        name=Identifier("node-image"),
        image_repository=dockerhub_ecr_image_uri("node"),
        image_tag=node_image_tag,
        ecr_region=ECR_REGION,
    )

    # Define source and client repositories using parameters
    if source_repo_tag_regex:
        source_repository = git_repo(
            name=Identifier(source_repo_name),
            uri=source_repo_uri,
            version_type="tags",
            fetch_tags=True,
            tag_regex=source_repo_tag_regex,
        )
        # An ol-concourse without version_type support does not reject the
        # argument: git_repo forwards it through **kwargs onto the Resource, so
        # it lands as a top-level key, `source` never gets it, and the resource
        # silently falls back to versioning commits on `main` with no paths
        # filter -- republishing the client on every commit. Fail here instead.
        if "version_type" not in source_repository.source:
            msg = (
                f"{source_repo_name} asked to version on tags, but the installed "
                "ol-concourse dropped version_type from the resource source. "
                "Upgrade ol-concourse to a release that supports it."
            )
            raise RuntimeError(msg)
    else:
        source_repository = git_repo(
            name=Identifier(source_repo_name),
            uri=source_repo_uri,
            branch=source_repo_branch,
            paths=["openapi/specs/*.yaml"],
        )

    api_clients_repository = ssh_git_repo(
        name=Identifier(client_repo_name),
        uri=client_repo_uri,
        branch=client_repo_branch,
        private_key="((npm_publish.odlbot_private_ssh_key))",
    )

    # Define the 'generate-clients' job
    generate_clients_job = Job(
        name=Identifier("generate-clients"),
        plan=[
            GetStep(get=python_image.name),
            GetStep(get=source_repository.name, trigger=True),
            GetStep(get=api_clients_repository.name, trigger=False),
            GetStep(get=openapi_generator_image.name),
            _generate_apis_step(
                openapi_generator_image.name,
                Input(name=source_repository.name),
                api_clients_repository.name,
            ),
            LoadVarStep(
                load_var=Identifier(f"{source_repo_name}-git-rev"),
                # .git/ref, not .git/refs/heads/<branch>: the git resource
                # writes it under both version_types used here (commits and
                # tags), and a tag checkout is detached, so there is no branch
                # ref on disk to read. Note in_branches.sh does not write it,
                # so this would need revisiting for a branches-based config.
                file=f"{source_repository.name}/.git/ref",
                reveal=True,
            ),
            TaskStep(
                task="bump-version",
                image=python_image.name,
                config=TaskConfig(
                    platform="linux",
                    inputs=[Input(name=api_clients_repository.name)],
                    outputs=[Output(name=api_clients_repository.name)],
                    run=Command(
                        path="sh",
                        dir=api_clients_repository.name,
                        args=[
                            "-xc",
                            _read_script(bump_script),
                        ],
                    ),
                ),
            ),
            TaskStep(
                task="commit-changes",
                config=TaskConfig(
                    inputs=[Input(name=api_clients_repository.name)],
                    outputs=[Output(name=api_clients_repository.name)],
                    image_resource=AnonymousResource(
                        source={
                            "repository": dockerhub_ecr_image_uri(
                                "concourse/buildroot"
                            ),
                            "tag": "git",
                            "aws_region": ECR_REGION,
                        },
                        type="registry-image",
                    ),
                    platform="linux",
                    params={
                        "SOURCE_REPO_NAME": source_repo_name,
                        "OPEN_REV": f"((.:{source_repo_name}-git-rev))",
                    },
                    run=Command(
                        path="sh",
                        dir=api_clients_repository.name,
                        args=["-xc", _read_script(commit_script)],
                    ),
                ),
            ),
            PutStep(
                put=api_clients_repository.name,
                params={
                    "repository": api_clients_repository.name,
                    "tag": f"{api_clients_repository.name}/VERSION",
                },
            ),
        ],
    )

    # One publish job per subpath, each gated on the same successful
    # generate-clients run. A source repo publishing several specs generates
    # several packages out of the one client repo, and each is published from
    # its own directory; they share the repo-root VERSION the bump step
    # wrote, so a release moves them together. Separate jobs (rather than
    # sequential steps in one job) keep a failed package retryable on its
    # own -- npm rejects republishing an already-published name/version, so
    # retrying a single job that already published some packages would fail
    # before reaching the one that didn't.
    publish_jobs = [
        Job(
            name=Identifier(f"publish-{subpath}"),
            plan=[
                # An input to the build, never a reason to publish. VERSION
                # only moves when generate-clients bumps it, so a new image
                # digest would republish a version npm already has and fail
                # (403). The clients repo get below is the only trigger.
                GetStep(get=node_image.name),
                GetStep(
                    get=api_clients_repository.name,
                    passed=[generate_clients_job.name],
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier(f"publish-node-{subpath}"),
                    image=node_image.name,
                    config=TaskConfig(
                        platform="linux",
                        inputs=[Input(name=api_clients_repository.name)],
                        params={"NPM_TOKEN": "((npm_publish.npmjs_token))"},
                        run=Command(
                            path="sh",
                            dir=f"{api_clients_repository.name}/src/typescript/{subpath}",
                            args=["-xc", _read_script(publish_script)],
                        ),
                    ),
                ),
            ],
        )
        for subpath in client_repo_subpaths
    ]

    prerelease_resources = []
    prerelease_jobs = []
    if prerelease_branch:
        prerelease_source = git_repo(
            name=Identifier(f"{source_repo_name}-{prerelease_branch}"),
            uri=source_repo_uri,
            branch=prerelease_branch,
            paths=["openapi/specs/*.yaml"],
        )
        prerelease_rev = Identifier(f"{prerelease_source.name}-git-rev")
        prerelease_resources.append(prerelease_source)
        # Generated afresh in each job and never committed: the clients repo
        # get is only the generator configs and package skeleton.
        prerelease_jobs = [
            Job(
                name=Identifier(f"publish-next-{subpath}"),
                # In trigger order, so `next` ends on the newest spec even when
                # two merges land minutes apart.
                serial=True,
                plan=[
                    GetStep(get=openapi_generator_image.name),
                    # Not a trigger, for the same reason as the stable publish
                    # jobs: here a new digest would mint a prerelease for a
                    # spec that did not change.
                    GetStep(get=node_image.name),
                    GetStep(get=prerelease_source.name, trigger=True),
                    GetStep(get=api_clients_repository.name, trigger=False),
                    _generate_apis_step(
                        openapi_generator_image.name,
                        Input(name=prerelease_source.name, path=source_repo_name),
                        api_clients_repository.name,
                    ),
                    LoadVarStep(
                        load_var=prerelease_rev,
                        file=f"{prerelease_source.name}/.git/ref",
                        reveal=True,
                    ),
                    TaskStep(
                        task=Identifier(f"publish-next-{subpath}"),
                        image=node_image.name,
                        config=TaskConfig(
                            platform="linux",
                            inputs=[Input(name=api_clients_repository.name)],
                            params={
                                "NPM_TOKEN": "((npm_publish.npmjs_token))",
                                "SOURCE_SHA": f"((.:{prerelease_rev}))",
                            },
                            run=Command(
                                path="bash",
                                dir=f"{api_clients_repository.name}/src/typescript/{subpath}",
                                args=["-xc", _read_script(publish_prerelease_script)],
                            ),
                        ),
                    ),
                ],
            )
            for subpath in client_repo_subpaths
        ]

    # Construct the final pipeline
    return Pipeline(
        resources=[
            source_repository,
            api_clients_repository,
            openapi_generator_image,
            python_image,
            node_image,
            *prerelease_resources,
        ],
        jobs=[
            generate_clients_job,
            *publish_jobs,
            *prerelease_jobs,
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        required=True,
        choices=PIPELINE_CONFIGS.keys(),
        help="The pipeline variant configuration to generate.",
    )
    args = parser.parse_args()

    variant_config = PIPELINE_CONFIGS[args.variant]
    pipeline = generate_api_client_pipeline(**variant_config)
    # Print the generated pipeline definition JSON to stdout
    sys.stdout.write(
        pipeline_json_with_user_data(
            pipeline,
            user_data={
                "description": (
                    f"Generates the TypeScript API client for "
                    f"`{variant_config['source_repo_name']}` from its OpenAPI "
                    f"spec (`openapi/specs/*.yaml`) via openapi-generator, "
                    f"bumps and commits the version in "
                    f"`{variant_config['client_repo_name']}`, then publishes "
                    "the package to npm."
                    + (
                        " Spec changes on "
                        f"`{variant_config['prerelease_branch']}` also publish "
                        "an uncommitted prerelease under the npm `next` tag."
                        if variant_config.get("prerelease_branch")
                        else ""
                    )
                ),
                "team": "main",
                "category": "package-publishing",
            },
        )
    )
