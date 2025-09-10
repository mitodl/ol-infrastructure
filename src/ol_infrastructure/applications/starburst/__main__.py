import base64
from pathlib import Path
from typing import Any, NoReturn

import pulumi
import requests
import yaml
from pulumi import dynamic

# Configuration
config = pulumi.Config("starburst")
starburst_domain = config.get("starburst_domain")
client_id = config.require_secret("client_id")
client_secret = config.require_secret("client_secret")

# Warehouse configuration
warehouse_prefix = config.get("warehouse_prefix") or "ol_warehouse"
environments = config.get_object("environments") or ["production", "qa"]

# Path to dbt project
DBT_PROJECT_PATH = (
    config.get("dbt_project_path")
    or "https://raw.githubusercontent.com/mitodl/ol-data-platform/main/src/ol_dbt"
)


class DbtProjectParser:
    """Parse dbt project configuration to extract schema and grants information."""

    def __init__(self, project_path: str) -> None:
        self.project_path = project_path
        self.project_config = self._load_dbt_project()

    def _load_dbt_project(self) -> dict[str, Any]:
        """Load dbt_project.yml configuration from local path or remote URL."""
        if self.project_path.startswith(("http://", "https://")):
            # Remote URL
            dbt_project_url = f"{self.project_path}/dbt_project.yml"
            try:
                response = requests.get(dbt_project_url, timeout=30)
                response.raise_for_status()
                return yaml.safe_load(response.text)
            except (requests.RequestException, yaml.YAMLError) as e:
                msg = f"dbt_project.yml not found at {dbt_project_url}: {e}"
                raise FileNotFoundError(msg) from e
        else:
            # Local path
            dbt_project_file = Path(self.project_path) / "dbt_project.yml"
            if not dbt_project_file.exists():
                msg = f"dbt_project.yml not found at {dbt_project_file}"
                raise FileNotFoundError(msg)

            try:
                with dbt_project_file.open() as f:
                    return yaml.safe_load(f)
            except (OSError, yaml.YAMLError) as e:
                msg = f"Failed to read dbt_project.yml from {dbt_project_file}: {e}"
                raise FileNotFoundError(msg) from e

    def get_grants_by_domain(self) -> dict[str, dict[str, list[str]]]:
        """Extract grants by domain from dbt project configuration.

        Returns:
            dict[domain, dict[privilege_type, list[role_names]]]
            e.g., {"staging": {"Select": ["read_only_production", "reverse_etl"]}}
        """
        models_config = self.project_config.get("models", {})
        project_name = self.project_config.get("name", "open_learning")

        if project_name not in models_config:
            pulumi.log.warn(f"No models config found for project '{project_name}'")
            return {}

        project_models = models_config[project_name]
        return self._parse_grants_by_domain(project_models)

    def _parse_grants_by_domain(
        self, models_config: dict[str, Any]
    ) -> dict[str, dict[str, list[str]]]:
        """Parse grants configuration and organize by domain."""
        grants_by_domain = {}

        # Domain mapping: dbt model key -> (domain_name, actual_schema_name)
        # Most are the same, but 'marts' -> 'mart' is different
        domain_mappings = {
            "staging": ("staging", "staging"),
            "intermediate": ("intermediate", "intermediate"),
            "marts": ("marts", "mart"),  # ← Key difference: plural -> singular
            "dimensional": ("dimensional", "dimensional"),
            "external": ("external", "external"),
            "reporting": ("reporting", "reporting"),
            "migration": ("migration", "migration"),
        }

        # Process domain-specific grants
        for dbt_key, (domain_name, _actual_schema) in domain_mappings.items():
            if dbt_key in models_config:
                domain_config = models_config[dbt_key]
                if isinstance(domain_config, dict) and "+grants" in domain_config:
                    grants = domain_config["+grants"]
                    grants_by_domain[domain_name] = self._normalize_grants(grants)

        # Apply global grants to all domains
        if "+grants" in models_config:
            global_grants = models_config["+grants"]
            normalized_global = self._normalize_grants(global_grants)

            for domain_name, _actual_schema in domain_mappings.values():
                if domain_name not in grants_by_domain:
                    grants_by_domain[domain_name] = {}

                # Merge global grants with domain-specific grants
                for privilege_type, roles in normalized_global.items():
                    if privilege_type not in grants_by_domain[domain_name]:
                        grants_by_domain[domain_name][privilege_type] = []
                    grants_by_domain[domain_name][privilege_type].extend(roles)
                    # Remove duplicates
                    grants_by_domain[domain_name][privilege_type] = list(
                        set(grants_by_domain[domain_name][privilege_type])
                    )

        return grants_by_domain

    def _normalize_grants(self, grants: dict[str, Any]) -> dict[str, list[str]]:
        """Normalize grants to standard privilege names."""
        normalized = {}

        # Map dbt grant names to Starburst privilege names
        privilege_mapping = {
            "select": "Select",
            "insert": "Insert",
            "update": "Update",
            "delete": "Delete",
            "all privileges": "All",
        }

        for dbt_privilege, roles in grants.items():
            starburst_privilege = privilege_mapping.get(
                dbt_privilege.lower(), dbt_privilege
            )
            if isinstance(roles, list):
                normalized[starburst_privilege] = roles
            elif isinstance(roles, str):
                normalized[starburst_privilege] = [roles]

        return normalized

    def get_data_domains(self) -> dict[str, list[str]]:
        """Get data domains with their corresponding schema names from dbt project."""
        domain_to_schemas: dict[str, list[str]] = {}
        models_config = self.project_config.get("models", {})
        project_name = self.project_config.get("name", "open_learning")
        project_models = models_config.get(project_name, {})

        # Domain mapping: dbt model key -> domain_name
        domain_mappings = {
            "staging": "staging",
            "intermediate": "intermediate",
            "marts": "mart",  # ← Key difference: plural -> singular
            "dimensional": "dimensional",
            "external": "external",
            "reporting": "reporting",
            "migration": "migration",
        }

        for dbt_key, domain_name in domain_mappings.items():
            model_config = project_models.get(dbt_key, {})
            if isinstance(model_config, dict) and "+schema" in model_config:
                base_schema = model_config["+schema"]
                domain_to_schemas[domain_name] = [
                    f"{warehouse_prefix}_{env}_{base_schema}" for env in environments
                ]

        # Add 'raw' domain which is not defined in dbt models section
        if "raw" not in domain_to_schemas:
            domain_to_schemas["raw"] = [
                f"{warehouse_prefix}_{env}_raw" for env in environments
            ]

        return domain_to_schemas


class StarburstAPIClient:
    """Simple HTTP client for Starburst API calls."""

    def __init__(self, domain: str, client_id: str, client_secret: str) -> None:
        self.domain = domain
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = f"https://{domain}"
        self._access_token: str | None = None
        self._session = requests.Session()

    def _get_access_token(self) -> str:
        """Get OAuth access token for API calls."""
        if self._access_token:
            return self._access_token

        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        response = self._session.post(
            f"{self.base_url}/oauth/v2/token",
            headers={
                "Authorization": f"Basic {encoded_credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
            timeout=30,
        )
        response.raise_for_status()

        token_data = response.json()
        self._access_token = token_data["access_token"]
        return self._access_token

    def _make_authenticated_request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> requests.Response:
        """Make an authenticated request to the Starburst API."""
        token = self._get_access_token()
        headers = kwargs.pop("headers", {})
        headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = self._session.request(
            method, url, headers=headers, timeout=30, **kwargs
        )
        response.raise_for_status()
        return response

    def get_clusters(self) -> dict[str, Any]:
        """Fetch clusters from Starburst API."""
        response = self._make_authenticated_request("GET", "/public/api/v1/cluster")
        return response.json()

    def get_cluster_ids_by_name(self, cluster_names: list[str]) -> dict[str, str]:
        """Get cluster IDs for specific cluster names."""
        clusters_data = self.get_clusters()
        cluster_name_to_id = {}

        clusters_list = clusters_data.get("result", [])
        for cluster in clusters_list:
            cluster_id = cluster.get("clusterId")
            cluster_name = cluster.get("name")

            if cluster_name in cluster_names and cluster_id:
                cluster_name_to_id[cluster_name] = cluster_id

        return cluster_name_to_id

    def create_role(self, role_name: str, description: str = "") -> dict[str, Any]:
        """Create a new role in Starburst."""
        payload = {"roleName": role_name, "roleDescription": description}
        response = self._make_authenticated_request(
            "POST", "/public/api/v1/role", json=payload
        )
        return response.json()

    def grant_privilege(
        self, role_id: str, privilege: dict[str, Any]
    ) -> dict[str, Any]:
        """Grant a privilege to a role."""
        response = self._make_authenticated_request(
            "POST", f"/public/api/v1/role/{role_id}/privilege", json=privilege
        )
        return response.json()

    def delete_role(self, role_id: str) -> bool:
        """Delete a role."""
        try:
            self._make_authenticated_request("DELETE", f"/public/api/v1/role/{role_id}")
        except requests.RequestException:
            return False
        else:
            return True


def _raise_role_creation_error(role_name: str) -> NoReturn:
    """Raise ValueError for missing role ID."""
    msg = f"No roleId returned for role {role_name}"
    raise ValueError(msg)


class StarburstRoleProvider(dynamic.ResourceProvider):
    """Pulumi Dynamic Resource Provider for Starburst roles."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        """Create a Starburst role and its privileges."""
        try:
            api_client = StarburstAPIClient(
                domain=props["domain"],
                client_id=props["client_id"],
                client_secret=props["client_secret"],
            )

            # Create the role
            role_response = api_client.create_role(
                role_name=props["role_name"], description=props.get("description", "")
            )

            role_id = role_response.get("roleId")
            if not role_id:
                _raise_role_creation_error(props["role_name"])

            # Grant privileges to the role
            privilege_responses = []
            for privilege in props.get("privileges", []):
                try:
                    privilege_response = api_client.grant_privilege(role_id, privilege)
                    privilege_responses.append(privilege_response)
                except requests.RequestException as e:
                    privilege_name = privilege.get("privilege")
                    role_name = props["role_name"]
                    pulumi.log.warn(
                        f"Failed to grant privilege {privilege_name} "
                        f"to role {role_name}: {e}"
                    )

            return dynamic.CreateResult(
                id_=role_id,
                outs={
                    "role_id": role_id,
                    "role_name": props["role_name"],
                    "description": props.get("description", ""),
                    "privileges_count": len(privilege_responses),
                    "status": "created",
                },
            )

        except (requests.RequestException, ValueError, KeyError) as e:
            msg = f"Failed to create role {props['role_name']}: {e}"
            raise RuntimeError(msg) from e

    def delete(self, role_id: str, props: dict[str, Any]) -> None:
        """Delete a Starburst role."""
        try:
            api_client = StarburstAPIClient(
                domain=props["domain"],
                client_id=props["client_id"],
                client_secret=props["client_secret"],
            )

            success = api_client.delete_role(role_id)
            if not success:
                pulumi.log.warn(
                    f"Failed to delete role {role_id}, it may have already been deleted"
                )

        except requests.RequestException as e:
            pulumi.log.warn(f"Error during role deletion {role_id}: {e}")


class StarburstRole(dynamic.Resource):
    """A Starburst role resource."""

    def __init__(
        self,
        name: str,
        props: dict[str, Any],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(StarburstRoleProvider(), name, props, opts)


def _build_base_privileges(
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


def _build_cluster_privileges(
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


def _build_data_privileges_from_dbt_grants(
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


def generate_role_definitions(
    dbt_parser: DbtProjectParser,
    cluster_ids: dict[str, str],
    roles_config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Generate Starburst role definitions based on configuration and dbt structure."""
    data_domains = dbt_parser.get_data_domains()
    grants_by_domain = dbt_parser.get_grants_by_domain()
    roles = {}

    for role_name, role_config in roles_config.items():
        privileges = []

        # Process base privileges
        for base_privilege_group in role_config.get("base_privileges", []):
            privileges.extend(_build_base_privileges(base_privilege_group))

        # Add cluster access privileges
        clusters_config = role_config.get("clusters", [])
        cluster_privileges = _build_cluster_privileges(
            clusters_config, cluster_ids, role_name
        )
        privileges.extend(cluster_privileges)

        # Add data privileges from dbt grants
        data_privileges = _build_data_privileges_from_dbt_grants(
            role_name, data_domains, grants_by_domain
        )
        privileges.extend(data_privileges)

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

    # Return dummy cluster IDs for preview
    if pulumi.runtime.is_dry_run():
        pulumi.log.info(
            f"Preview mode: using dummy cluster IDs for {list(all_cluster_names)}"
        )
        dummy_ids = {}
        for cluster_name in all_cluster_names:
            dummy_ids[cluster_name] = f"preview-{cluster_name.replace('-', '_')}-id"
        return dummy_ids

    try:
        api_client = StarburstAPIClient(
            domain=starburst_domain,
            client_id=client_id_val,
            client_secret=client_secret_val,
        )

        pulumi.log.info(f"Fetching cluster IDs for: {list(all_cluster_names)}")
        cluster_ids = api_client.get_cluster_ids_by_name(list(all_cluster_names))
    except requests.RequestException as e:
        pulumi.log.warn(f"Could not retrieve cluster data: {e}")
        return {}
    else:
        pulumi.log.info(f"Found cluster IDs: {cluster_ids}")
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
