from typing import Union

from concourse.lib.models.pipeline import Identifier, Resource
from concourse.lib.models.resource import Git


def git_repo(
    name: Identifier, uri: str, branch: str = "main", paths: list[str] = None
) -> Resource:
    return Resource(
        name=name,
        type="git",
        icon="git",
        source=Git(uri=uri, branch=branch, paths=paths),
    )


def github_release(name: Identifier, owner: str, repository: str) -> Resource:
    """Generate a github-release resource for the given owner/repository.

    :param name: The name of the resource. This will get used across subsequent pipeline steps that reference this resource.
    :type name: Identifier
    :param owner: The owner of the repository (e.g. the GitHub user or organization)
    :type owner: str
    :param repository: The name of the repository as it appears in GitHub
    :type repository: str
    :returns: A configured Concourse resource object that can be used in a pipeline.
    :rtype: Resource
    """
    return Resource(
        name=name,
        type="github-release",
        icon="github",
        check_every="24h",
        source={"repository": repository, "owner": owner, "release": True},
    )


def amazon_ami(
    name: Identifier, filters: dict[str, Union[str, bool]], region: str = "us-east-1"
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
