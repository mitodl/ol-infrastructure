"""dbt project parsing utilities."""

from pathlib import Path
from typing import Any

import requests
import yaml


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

    def get_project_name(self) -> str:
        """Get the dbt project name."""
        return self.project_config.get("name", "")

    def get_project_version(self) -> str:
        """Get the dbt project version."""
        return self.project_config.get("version", "")

    def get_profile_name(self) -> str:
        """Get the dbt profile name."""
        return self.project_config.get("profile", "")

    def get_model_paths(self) -> list[str]:
        """Get the model paths configuration."""
        return self.project_config.get("model-paths", ["models"])

    def get_variables(self) -> dict[str, Any]:
        """Get dbt project variables."""
        return self.project_config.get("vars", {})

    def get_models_config(self) -> dict[str, Any]:
        """Get the models configuration section."""
        return self.project_config.get("models", {})

    def get_project_models_config(
        self, project_name: str | None = None
    ) -> dict[str, Any]:
        """Get models configuration for a specific project."""
        if project_name is None:
            project_name = self.get_project_name()

        models_config = self.get_models_config()
        return models_config.get(project_name, {})

    def get_grants_by_domain(
        self, project_name: str | None = None
    ) -> dict[str, dict[str, list[str]]]:
        """Extract grants by domain from dbt project configuration.

        Args:
            project_name: dbt project name, defaults to project name from config

        Returns:
            dict[domain, dict[privilege_type, list[role_names]]]
            e.g., {"staging": {"Select": ["read_only_production", "reverse_etl"]}}
        """
        project_models = self.get_project_models_config(project_name)
        return self._parse_grants_by_domain(project_models)

    def get_data_domains(
        self,
        warehouse_prefix: str = "ol_warehouse",
        environments: list[str] | None = None,
        project_name: str | None = None,
    ) -> dict[str, list[str]]:
        """Get data domains with their corresponding schema names from dbt project.

        Args:
            warehouse_prefix: Prefix for warehouse schema names
            environments: List of environments (e.g., ["production", "qa"])
            project_name: dbt project name, defaults to project name from config
        """
        if environments is None:
            environments = ["production", "qa"]

        domain_to_schemas: dict[str, list[str]] = {}
        project_models = self.get_project_models_config(project_name)

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

        # Map dbt grant names to standard privilege names
        privilege_mapping = {
            "select": "Select",
            "insert": "Insert",
            "update": "Update",
            "delete": "Delete",
            "all privileges": "All",
        }

        for dbt_privilege, roles in grants.items():
            standard_privilege = privilege_mapping.get(
                dbt_privilege.lower(), dbt_privilege
            )
            if isinstance(roles, list):
                normalized[standard_privilege] = roles
            elif isinstance(roles, str):
                normalized[standard_privilege] = [roles]

        return normalized
