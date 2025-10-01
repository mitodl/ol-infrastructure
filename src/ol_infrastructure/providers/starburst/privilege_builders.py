"""Privilege definition builders for Starburst roles."""

from typing import Any

import pulumi


def build_base_privileges(
    base_privilege_group: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build base privilege definitions from configuration group.

    Only handles Account and Function level privileges.
    Catalog/Schema/Table privileges are managed by dbt.
    """
    privileges: list[dict[str, Any]] = []
    entity_kind = base_privilege_group["entity_kind"]
    privilege_list = base_privilege_group.get("privileges", [])

    # Only process Account and Function level privileges
    if entity_kind not in ["Account", "Function"]:
        pulumi.log.warn(
            f"Skipping {entity_kind} privileges - data privileges are managed by dbt"
        )
        return privileges

    for privilege_config in privilege_list:
        if isinstance(privilege_config, str):
            privilege_name = privilege_config
            grant_option = False
        else:
            privilege_name = privilege_config["name"]
            grant_option = privilege_config.get("with_grant_option", False)

        privilege_def = {
            "privilege": privilege_name,
            "entity_kind": entity_kind,
            "grant_option": grant_option,
        }

        # For Function privileges, default to wildcard
        if entity_kind == "Function":
            privilege_def["function_name"] = "*"

        privileges.append(privilege_def)

    return privileges


def build_cluster_privileges(
    clusters_config: list[dict[str, Any]], cluster_ids: dict[str, str], role_name: str
) -> list[dict[str, Any]]:
    """Build cluster privilege definitions."""
    privileges = []

    for cluster_config in clusters_config:
        cluster_name = cluster_config["name"]
        cluster_privileges = cluster_config.get("privileges", [])

        cluster_id = cluster_ids.get(cluster_name)
        if not cluster_id:
            pulumi.log.warn(
                f"No cluster ID found for '{cluster_name}' in role '{role_name}'"
            )
            continue

        for privilege_config in cluster_privileges:
            if isinstance(privilege_config, str):
                privilege_name = privilege_config
                grant_option = False
            else:
                privilege_name = privilege_config["name"]
                grant_option = privilege_config.get("with_grant_option", False)

            privileges.append(
                {
                    "privilege": privilege_name,
                    "entity_kind": "Cluster",
                    "entity_id": cluster_id,
                    "grant_option": grant_option,
                }
            )

    return privileges


def build_data_privileges_from_dbt_grants(
    role_name: str,
    data_domains: dict[str, dict[str, Any]],
    grants_by_domain: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """Build data privileges based on dbt grants configuration.

    This function is only called when manage_data_privileges is True.
    Otherwise, dbt manages these privileges automatically through its own
    grant mechanism.
    """
    privileges = []

    # Minimum parts needed for a valid object path
    MIN_OBJECT_PATH_PARTS = 2

    # For each data domain, check if the role has grants
    for domain_key, domain_config in data_domains.items():
        if role_name not in grants_by_domain.get(domain_key, {}):
            continue

        # Get privileges granted to this role for this domain
        privilege_list = grants_by_domain[domain_key][role_name]
        object_path = domain_config["object_path"]

        # Parse the object path (catalog.schema.table)
        parts = object_path.split(".")
        if len(parts) < MIN_OBJECT_PATH_PARTS:
            pulumi.log.warn(f"Invalid object path in dbt grants: {object_path}")
            continue

        catalog_name = parts[0]
        schema_name = parts[1]
        table_name = (
            parts[MIN_OBJECT_PATH_PARTS] if len(parts) > MIN_OBJECT_PATH_PARTS else None
        )

        for privilege_name in privilege_list:
            # Determine the entity type based on whether we have a table
            if table_name:
                entity_kind = "Table"
                entity_id = f"{catalog_name}.{schema_name}.{table_name}"
            else:
                entity_kind = "Schema"
                entity_id = f"{catalog_name}.{schema_name}"

            privileges.append(
                {
                    "entity_kind": entity_kind,
                    "entity_id": entity_id,
                    "privilege": privilege_name,
                    "grant_option": False,
                }
            )

    return privileges
