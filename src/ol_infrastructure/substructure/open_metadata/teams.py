"""Team names shared between the OpenMetadata jobs."""

import ast


def declared_team_names(server_config_script: str) -> list[str]:
    """Return the team names om_server_config.py creates.

    Read from the script's source because importing it reads the job's
    environment variables. Only the annotated ``TEAM_CONFIG`` literal is read;
    the tests compare it with the imported dict, so a later assignment to it
    fails CI instead of having its team dropped from Keycloak.

    :param server_config_script: The source of om_server_config.py.
    :returns: The team names, sorted.
    :rtype: list[str]
    :raises ValueError: If the script no longer assigns ``TEAM_CONFIG``.
    """
    for node in ast.parse(server_config_script).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TEAM_CONFIG"
            and node.value is not None
        ):
            return sorted(ast.literal_eval(node.value))
    msg = "om_server_config.py no longer assigns TEAM_CONFIG"
    raise ValueError(msg)
