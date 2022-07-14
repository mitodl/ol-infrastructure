from concourse.lib.models import Identifier, Resource


def git_repo(name: str, uri: str, branch: str = "main"):
    return Resource(
        name=Identifier(name),
        type="git",
        icon="git",
        source={"uri": uri, "branch": branch},
    )
