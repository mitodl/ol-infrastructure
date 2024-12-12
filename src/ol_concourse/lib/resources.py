from typing import Literal, Optional, Union

from ol_concourse.lib.models.pipeline import Identifier, RegistryImage, Resource
from ol_concourse.lib.models.resource import Git


def git_repo(  # noqa: PLR0913
    name: Identifier,
    uri: str,
    branch: str = "main",
    check_every: str = "60s",
    paths: Optional[list[str]] = None,
    depth: Optional[int] = None,
    fetch_tags: bool = False,  # noqa: FBT001, FBT002
    tag_regex: Optional[str] = None,
    **kwargs,
) -> Resource:
    return Resource(
        name=name,
        type="git",
        icon="git",
        check_every=check_every,
        source=Git(
            uri=uri,
            branch=branch,
            paths=paths,
            depth=depth,
            fetch_tags=fetch_tags,
            tag_regex=tag_regex,
        ).model_dump(exclude_none=True),
        **kwargs,
    )


def ssh_git_repo(
    name: Identifier,
    uri: str,
    private_key: str,
    branch: str = "main",
    paths: Optional[list[str]] = None,
) -> Resource:
    return Resource(
        name=name,
        type="git",
        icon="git",
        source=Git(
            uri=uri, branch=branch, paths=paths, private_key=private_key
        ).model_dump(exclude_none=True),
    )


def github_release(  # noqa: PLR0913
    name: Identifier,
    owner: str,
    repository: str,
    github_token: str = "((github.public_repo_access_token))",  # noqa: S107
    tag_filter: Optional[str] = None,
    order_by: Optional[Literal["time", "version"]] = None,
) -> Resource:
    """Generate a github-release resource for the given owner/repository.

    :param name: The name of the resource.  This will get used across subsequent
        pipeline steps that reference this resource.
    :param owner: The owner of the repository (e.g. the GitHub user or organization)
    :param repository: The name of the repository as it appears in GitHub
    :param github_token: A personal access token with `public_repo` scope to increase
        the rate limit for checking versions.
    :param tag_filter: A regular expression used to filter the repository tags to
        include in the version results.
    :param order_by: Indicate whether to order by version number or time.  Primarily
        useful when in combination with `tag_filter`.

    :returns: A configured Concourse resource object that can be used in a pipeline.

    :rtype: Resource
    """
    release_config = {
        "repository": repository,
        "owner": owner,
        "release": True,
    }
    if tag_filter:
        release_config["tag_filter"] = tag_filter
    if github_token:
        release_config["access_token"] = github_token
    if order_by:
        release_config["order_by"] = order_by
    return Resource(
        name=name,
        type="github-release",
        icon="github",
        check_every="24h",
        source=release_config,
    )


def github_issues(  # noqa: PLR0913
    name: Identifier,
    repository: str,
    issue_prefix: str,
    auth_method: Literal["token", "app"] = "token",
    gh_host: Optional[str] = "https://github.mit.edu/api/v3",
    access_token: str = "((github.issues_resource_access_token))",  # noqa: S107
    app_id: Optional[str] = None,
    app_installation_id: Optional[str] = None,
    private_ssh_key: Optional[str] = None,
    issue_state: Literal["open", "closed"] = "closed",
    labels: Optional[list[str]] = None,
    assignees: Optional[list[str]] = None,
    issue_title_template: Optional[str] = None,
    issue_body_template: Optional[str] = None,
) -> Resource:
    """Generate a github-issue resource for the given owner/repository.

    :param name: The name of the resource.  This will get used across subsequent
        pipeline steps that reference this resource.
    :param repository: The name of the repository as it appears in GitHub
    :param github_token: A personal access token with `public_repo` scope to increase
        the rate limit for checking versions.
    :param issue_prefix: A string tobe used to match an issue in the repository for
     the workflow to detect or act upon.

    :returns: A configured Concourse issue object that can be used in a pipeline.

    :rtype: Resource
    """
    issue_config = {
        "auth_method": auth_method,
        "assignees": assignees,
        "issue_body_template": issue_body_template,
        "issue_prefix": issue_prefix,
        "issue_state": issue_state,
        "issue_title_template": issue_title_template,
        "labels": labels,
        "repository": repository,
    }
    if gh_host:
        issue_config["gh_host"] = gh_host
    if auth_method == "token":
        issue_config["access_token"] = access_token
    else:
        issue_config["app_id"] = app_id
        issue_config["app_installation_id"] = app_installation_id
        issue_config["private_ssh_key"] = private_ssh_key
    return Resource(
        name=name,
        type="github-issues",
        icon="github",
        check_every="15m",
        expose_build_created_by=True,
        source={k: v for k, v in issue_config.items() if v is not None},
    )


def hashicorp_release(name: Identifier, project: str) -> Resource:
    """Generate a hashicorp-release resource for the given application.  # noqa: DAR201

    :param name: The name of the resourc. This will get used across subsequent
        pipeline steps taht reference this resource.
    :type name: Identifier
    :param project: The name of the hashicorp project to check for a release of.
    :type project: str
    """
    return Resource(
        name=name,
        type="hashicorp-release",
        icon="lock-check",
        check_every="24h",
        source={"project": project},
    )


def amazon_ami(
    name: Identifier,
    filters: dict[str, Union[str, bool]],
    region: str = "us-east-1",
) -> Resource:
    return Resource(
        name=name,
        type="amazon-ami",
        icon="server",
        check_every="30m",
        source={
            "region": region,
            "filters": filters,
        },
    )


def pulumi_provisioner(
    name: Identifier, project_name: str, project_path: str
) -> Resource:
    return Resource(
        name=name,
        type="pulumi-provisioner",
        icon="cloud-braces",
        source={
            "env_pulumi": {"AWS_SHARED_CREDENTIALS_FILE": "aws_creds/credentials"},
            "action": "update",
            "project_name": project_name,
            "source_dir": project_path,
        },
    )


def pypi(
    name: Identifier,
    package_name: str,
    username: str = "((pypi_creds.username))",
    password: str = "((pypi_creds.password))",  # noqa: S107
    check_every: str = "24h",
) -> Resource:
    return Resource(
        name=name,
        type="pypi",
        icon="language-python",
        check_every=check_every,
        source={
            "name": package_name,
            "packaging": "any",
            "repository": {
                "username": username,
                "password": password,
            },
        },
    )


def schedule(
    name: Identifier,
    interval: Optional[str] = None,
    start: Optional[str] = None,
    stop: Optional[str] = None,
    days: Optional[list[str]] = None,
) -> Resource:
    return Resource(
        name=name,
        type="time",
        icon="clock",
        source={
            "interval": interval,
            "start": start,
            "stop": stop,
            "days": days,
        },
    )


def registry_image(  # noqa: PLR0913
    name: Identifier,
    image_repository: str,
    image_tag: Optional[str] = None,
    variant: Optional[str] = None,
    tag_regex: Optional[str] = None,
    username=None,
    password=None,
) -> Resource:
    image_source = RegistryImage(
        repository=image_repository, tag=image_tag or "latest"
    ).model_dump()
    if username and password:
        image_source["username"] = username
        image_source["password"] = password
    if variant:
        image_source["variant"] = variant
    if tag_regex:
        image_source["tag_regex"] = tag_regex
    return Resource(
        name=name,
        type="registry-image",
        source=image_source,
    )


# https://github.com/arbourd/concourse-slack-alert-resource
# We use only a very basic implementation of this notification framework
def slack_notification(name: Identifier, url: str) -> Resource:
    return Resource(
        name=name, type="slack-notification", source={"url": url, "disabled": False}
    )


def s3_object(
    name: Identifier,
    bucket: str,
    object_path: str | None = None,
    object_regex: str | None = None,
):
    return Resource(
        name=name,
        type="s3",
        icon="bucket",
        source={
            "bucket": bucket,
            "regexp": object_regex,
            "versioned_file": object_path,
        },
    )


# This resource type also supports s3, gcs and others. We can create those later.
def git_semver(  # noqa: PLR0913
    name: str,
    uri: str,
    branch: str,
    file: str,
    private_key: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    git_user: Optional[str] = None,
    depth: Optional[int] = None,
    skip_ssl_verification: bool = False,  # noqa: FBT001, FBT002
    commit_message: Optional[str] = None,
    initial_version: str = "0.0.0",
) -> Resource:
    return Resource(
        name=name,
        type="semver",
        icon="version",
        source={
            "initial_version": initial_version,
            "driver": "git",
            "uri": uri,
            "branch": branch,
            "file": file,
            "private_key": private_key,
            "username": username,
            "password": password,
            "git_user": git_user,
            "depth": depth,
            "skip_ssl_verification": skip_ssl_verification,
            "commit_message": commit_message,
        },
    )
