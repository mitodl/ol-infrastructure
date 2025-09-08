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

# Path to dbt project
DBT_PROJECT_PATH = (
    config.get("dbt_project_path")
    or "https://raw.githubusercontent.com/mitodl/ol-data-platform/main/src/ol_dbt"
)


class DbtProjectParser:
    """Parse dbt project configuration to extract schema and model information."""

    def __init__(self, project_path: str) -> None:
        self.project_path = project_path
        self.project_config = self._load_dbt_project()
        self.models_config = self.project_config.get("models", {})

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

    def get_schema_mappings(self) -> dict[str, str]:
        """Extract schema mappings from dbt project configuration."""
        schema_mappings = {}

        # Get the main project name
        project_name = self.project_config.get("name", "ol_dbt")

        # Parse model configurations - try both direct project name and nested structure
        if project_name in self.models_config:
            models = self.models_config[project_name]
            schema_mappings.update(self._parse_model_schemas(models))

        # Also check for models at the root level
        for key, value in self.models_config.items():
            if isinstance(value, dict) and key != project_name:
                schema_mappings.update(self._parse_model_schemas(value, key))

        # If no schemas found, log a warning and return empty dict
        if not schema_mappings:
            pulumi.log.warn("No schema mappings found in dbt project configuration")
            pulumi.log.warn("Roles will only have base privileges and cluster access")

        return schema_mappings

    def _parse_model_schemas(
        self, models_config: dict[str, Any], prefix: str = ""
    ) -> dict[str, str]:
        """Recursively parse model schema configurations."""
        schema_mappings = {}

        for key, value in models_config.items():
            current_path = f"{prefix}.{key}" if prefix else key

            if isinstance(value, dict):
                # Check if this level has a schema definition
                if "schema" in value:
                    schema_mappings[current_path] = value["schema"]
                elif "+schema" in value:  # Alternative dbt syntax
                    schema_mappings[current_path] = value["+schema"]

                # Recursively parse nested configurations
                nested_mappings = self._parse_model_schemas(value, current_path)
                schema_mappings.update(nested_mappings)

        return schema_mappings

    def get_data_domains(self) -> dict[str, list[str]]:
        """Group schemas by data domain based on dbt project structure."""
        schema_mappings = self.get_schema_mappings()

        data_domains: dict[str, list[str]] = {
            "staging": [],
            "intermediate": [],
            "marts": [],
            "dimensional": [],
            "raw": [],
        }

        for path, schema in schema_mappings.items():
            if "staging" in path.lower() or "stg_" in schema.lower():
                data_domains["staging"].append(schema)
            elif "intermediate" in path.lower() or "int_" in schema.lower():
                data_domains["intermediate"].append(schema)
            elif "marts" in path.lower() or "mart_" in schema.lower():
                data_domains["marts"].append(schema)
            elif "dimensional" in path.lower() or "dimensional" in schema.lower():
                data_domains["dimensional"].append(schema)
            elif "raw" in path.lower() or "raw" in schema.lower():
                data_domains["raw"].append(schema)
            else:
                # Default unknown schemas to staging
                data_domains["staging"].append(schema)

        return data_domains


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

        # Extract clusters from API response
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
                    # Log privilege failures but don't fail the entire resource
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


def generate_role_definitions(
    dbt_parser: DbtProjectParser,
    cluster_ids: dict[str, str],
    roles_config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Generate Starburst role definitions based on configuration and dbt structure."""

    data_domains = dbt_parser.get_data_domains()
    roles = {}

    for role_name, role_config in roles_config.items():
        # Start with base privileges from config
        privileges = list(role_config.get("base_privileges", []))

        # Add cluster access privileges for multiple clusters
        cluster_names = role_config.get("clusters", [])
        if isinstance(cluster_names, str):  # Handle single cluster as string
            cluster_names = [cluster_names]

        for cluster_name in cluster_names:
            cluster_id = cluster_ids.get(cluster_name)

            if cluster_id:
                privileges.append(
                    {
                        "privilege": "UseCluster",
                        "entity_kind": "Cluster",
                        "entity_id": cluster_id,
                    }
                )
            else:
                pulumi.log.warn(
                    f"No cluster ID found for cluster '{cluster_name}' "
                    f"in role '{role_name}'"
                )

        # Add data access privileges based on domains
        for data_access in role_config.get("data_access", []):
            domain = data_access["domain"]
            privilege_types = data_access.get("privileges", [])

            if not privilege_types:
                pulumi.log.warn(
                    f"No privileges found for domain '{domain}' in role '{role_name}'"
                )
                continue

            # Get schemas for this domain
            schemas = data_domains.get(domain, [])

            for schema in schemas:
                for privilege_type in privilege_types:
                    if privilege_type == "Select":
                        privilege_def = {
                            "privilege": privilege_type,
                            "entity_kind": "Column",
                            "schema_name": schema,
                            "table_name": "*",
                            "column_name": "*",
                        }
                    else:
                        privilege_def = {
                            "privilege": privilege_type,
                            "entity_kind": "Table",
                            "schema_name": schema,
                            "table_name": "*",
                        }

                    privileges.append(privilege_def)

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
        if isinstance(clusters, str):
            clusters = [clusters]
        all_cluster_names.update(clusters)

    # Only fetch cluster IDs during actual deployment, not preview
    if pulumi.runtime.is_dry_run():
        pulumi.log.info(
            f"Preview mode: using dummy cluster IDs for {list(all_cluster_names)}"
        )
        # Return dummy cluster IDs for preview
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
        pulumi.log.info(f"Found cluster IDs: {cluster_ids}")
    except requests.RequestException as e:
        pulumi.log.warn(f"Could not retrieve cluster data: {e}")
        return {}
    else:
        return cluster_ids


def _prepare_role_definitions(
    data: tuple[dict[str, str], str, str],
    dbt_parser: DbtProjectParser,
    roles_config: dict[str, Any],
) -> dict[str, Any]:
    """Prepare role definitions from cluster data."""
    cluster_id_map, client_id_val, client_secret_val = data

    roles_definitions = generate_role_definitions(
        dbt_parser, cluster_id_map, roles_config
    )

    # Log what would be created during preview
    for role_name, role_definition in roles_definitions.items():
        privilege_count = len(role_definition.get("privileges", []))
        pulumi.log.info(
            f"Will create role '{role_name}' with {privilege_count} privileges"
        )

    return {
        "roles_definitions": roles_definitions,
        "client_id": client_id_val,
        "client_secret": client_secret_val,
    }


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

        schema_mappings = dbt_parser.get_schema_mappings()
        pulumi.log.info(
            f"Discovered {len(schema_mappings)} schema mappings from dbt project"
        )

    except FileNotFoundError as e:
        pulumi.log.error(f"Failed to parse dbt project: {e}")
        return

    # Get cluster IDs
    cluster_ids = pulumi.Output.all(client_id, client_secret).apply(
        lambda secrets: _get_cluster_ids(secrets, roles_config, starburst_domain)
    )

    # Prepare role definitions
    prepared_data = pulumi.Output.all(cluster_ids, client_id, client_secret).apply(
        lambda data: _prepare_role_definitions(data, dbt_parser, roles_config)
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
