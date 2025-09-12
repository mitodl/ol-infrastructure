"""Privilege definition builders for Starburst roles."""

from typing import Any

import pulumi


def build_base_privileges(
    base_privilege_group: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build base privilege definitions from configuration group."""
    privileges = []
    entity_kind = base_privilege_group["entity_kind"]
    privilege_list = base_privilege_group.get("privileges", [])

    # Handle additional properties
    additional_props = {
        k: v
        for k, v in base_privilege_group.items()
        if k not in ["entity_kind", "privileges", "catalogs"]
    }

    # Handle multiple catalogs
    if "catalogs" in base_privilege_group:
        catalogs = base_privilege_group["catalogs"]
    elif "catalog_name" in base_privilege_group:
        catalogs = [base_privilege_group["catalog_name"]]
    else:
        catalogs = [None]

    for catalog in catalogs:
        catalog_props = additional_props.copy()
        if catalog:
            catalog_props["catalog_name"] = catalog

        for privilege_config in privilege_list:
            if isinstance(privilege_config, str):
                privilege_name = privilege_config
                grant_option = False
            else:
                privilege_name = privilege_config["name"]
                grant_option = privilege_config.get("grant", False)

            privilege_def = {
                "privilege": privilege_name,
                "entity_kind": entity_kind,
                "grant_option": grant_option,
                **catalog_props,
            }
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
                f"No cluster ID found - '{cluster_name}' in role '{role_name}'"
            )
            continue

        for privilege_config in cluster_privileges:
            if isinstance(privilege_config, str):
                privilege_name = privilege_config
                grant_option = False
            else:
                privilege_name = privilege_config["name"]
                grant_option = privilege_config.get("grant", False)

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
    data_domains: dict[str, list[str]],
    grants_by_domain: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """Build data privileges based on dbt grants configuration."""
    privileges: list[dict[str, Any]] = []

    for domain, schemas in data_domains.items():
        domain_grants = grants_by_domain.get(domain, {})

        for privilege_type, granted_roles in domain_grants.items():
            if role_name in granted_roles:
                for schema in schemas:
                    privilege_def = {
                        "privilege": privilege_type,
                        "entity_kind": "Column"
                        if privilege_type == "Select"
                        else "Table",
                        "schema_name": schema,
                        "table_name": "*",
                        "grant_option": False,
                    }

                    if privilege_type == "Select":
                        privilege_def["column_name"] = "*"

                    privileges.append(privilege_def)

    if not privileges:
        pulumi.log.info(f"No dbt grants found for role '{role_name}'")
    else:
        pulumi.log.info(
            f"Built {len(privileges)} privileges for role '{role_name}' from dbt grants"
        )

    return privileges
