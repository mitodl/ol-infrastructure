"""Starburst Galaxy role management application."""

from typing import Any

import pulumi
import requests

from ol_infrastructure.lib.data.dbt import DbtProjectParser
from ol_infrastructure.providers.starburst import (
    StarburstAPIClient,
    StarburstRole,
    build_base_privileges,
    build_cluster_privileges,
    build_data_privileges_from_dbt_grants,
)

# Configuration
config = pulumi.Config("starburst")
starburst_domain = config.require("domain")
client_id = config.require_secret("client_id")
client_secret = config.require_secret("client_secret")

# Warehouse configuration
warehouse_prefix = config.get("warehouse_prefix") or "ol_warehouse"
environments = config.get_object("environments") or ["production", "qa"]

# Data privilege management configuration
manage_data_privileges = config.get_bool("manage_data_privileges") or False

# Path to dbt project
DBT_PROJECT_PATH = (
    config.get("dbt_project_path")
    or "https://raw.githubusercontent.com/mitodl/ol-data-platform/main/src/ol_dbt"
)


def generate_role_definitions(
    dbt_parser: DbtProjectParser,
    cluster_ids: dict[str, str],
    roles_config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Generate Starburst role definitions based on configuration and dbt structure."""
    roles = {}

    # Only parse dbt grants if data privilege management is enabled
    if manage_data_privileges:
        data_domains = dbt_parser.get_data_domains(warehouse_prefix, environments)
        grants_by_domain = dbt_parser.get_grants_by_domain()
        pulumi.log.info(
            "Data privilege management enabled - will sync privileges from dbt grants"
        )
    else:
        data_domains = {}
        grants_by_domain = {}
        pulumi.log.info(
            "Data privilege management disabled - dbt handles data grants automatically"
        )

    for role_name, role_config in roles_config.items():
        privileges = []

        # Process base privileges
        for base_privilege_group in role_config.get("base_privileges", []):
            privileges.extend(build_base_privileges(base_privilege_group))

        # Add cluster access privileges
        clusters_config = role_config.get("clusters", [])
        cluster_privileges = build_cluster_privileges(
            clusters_config, cluster_ids, role_name
        )
        privileges.extend(cluster_privileges)

        # Add data privileges from dbt grants (only if enabled)
        if manage_data_privileges:
            data_privileges = build_data_privileges_from_dbt_grants(
                role_name, data_domains, grants_by_domain
            )
            privileges.extend(data_privileges)
            if data_privileges:
                pulumi.log.info(
                    f"Added {len(data_privileges)} data privileges for role "
                    f"'{role_name}'"
                )

        roles[role_name] = {
            "description": role_config.get("description", ""),
            "privileges": privileges,
        }

    return roles


def _get_cluster_ids(
    secrets: tuple[str, str],
    roles_config: dict[str, Any],
    starburst_domain: str,
) -> dict[str, str]:
    """Get cluster IDs for role configurations."""
    client_id_val, client_secret_val = secrets

    # Collect all cluster names referenced in role configurations
    all_cluster_names = set()
    for role_config in roles_config.values():
        clusters = role_config.get("clusters", [])
        for cluster in clusters:
            all_cluster_names.add(cluster["name"])

    # Only use dummy IDs during preview if we can't connect to the API
    # Don't check is_dry_run() - let the API call happen during pulumi up
    try:
        api_client = StarburstAPIClient(
            domain=starburst_domain,
            client_id=client_id_val,
            client_secret=client_secret_val,
        )

        pulumi.log.info(f"Fetching cluster IDs for: {list(all_cluster_names)}")
        cluster_ids = api_client.get_cluster_ids_by_name(list(all_cluster_names))
        pulumi.log.info(f"Found cluster IDs: {cluster_ids}")
    except requests.RequestException as e:
        # Only fall back to dummy IDs if API call fails
        pulumi.log.warn(f"Could not retrieve cluster data: {e}")

        # Return dummy IDs as fallback
        pulumi.log.info(f"Using dummy cluster IDs for {list(all_cluster_names)}")
        dummy_ids = {}
        for cluster_name in all_cluster_names:
            dummy_ids[cluster_name] = f"preview-{cluster_name.replace('-', '_')}-id"
        return dummy_ids
    else:
        return cluster_ids


def _create_roles(
    prepared: dict[str, Any], starburst_domain: str
) -> dict[str, StarburstRole]:
    """Create Starburst role resources."""
    roles_definitions = prepared["roles_definitions"]
    client_id_val = prepared["client_id"]
    client_secret_val = prepared["client_secret"]

    created_roles = {}
    for role_name, role_definition in roles_definitions.items():
        role_resource = StarburstRole(
            f"starburst-role-{role_name}",
            props={
                "domain": starburst_domain,
                "client_id": client_id_val,
                "client_secret": client_secret_val,
                "role_name": role_name,
                "description": role_definition.get("description", ""),
                "privileges": role_definition.get("privileges", []),
            },
        )

        created_roles[role_name] = role_resource

    return created_roles


def main() -> None:
    """Create Starburst roles based on dbt project configuration."""

    # Load role definitions from configuration
    roles_config = config.get_object("roles") or {}
    if not roles_config:
        pulumi.log.error("No roles configuration found in starburst:roles")
        return

    pulumi.log.info(f"Found {len(roles_config)} role definitions in configuration")

    # Parse dbt project
    try:
        dbt_parser = DbtProjectParser(DBT_PROJECT_PATH)
        pulumi.log.info("Successfully parsed dbt project configuration")
    except FileNotFoundError as e:
        pulumi.log.error(f"Failed to parse dbt project: {e}")
        return

    # Get cluster IDs and prepare role definitions
    cluster_ids = pulumi.Output.all(client_id, client_secret).apply(
        lambda secrets: _get_cluster_ids(secrets, roles_config, starburst_domain)
    )

    prepared_data = pulumi.Output.all(cluster_ids, client_id, client_secret).apply(
        lambda data: {
            "roles_definitions": generate_role_definitions(
                dbt_parser, data[0], roles_config
            ),
            "client_id": data[1],
            "client_secret": data[2],
        }
    )

    # Create role resources
    role_resources = prepared_data.apply(
        lambda prepared: _create_roles(prepared, starburst_domain)
    )

    # Export role information
    def export_role_data(roles: dict[str, StarburstRole]) -> None:
        for role_name, role_resource in roles.items():
            pulumi.export(f"{role_name}_role_id", role_resource.id)
            pulumi.export(f"{role_name}_status", "created")

    role_resources.apply(export_role_data)


if __name__ == "__main__":
    main()
