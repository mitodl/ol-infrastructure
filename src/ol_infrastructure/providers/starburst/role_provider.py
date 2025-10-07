"""Starburst role Pulumi dynamic provider."""

from typing import Any, NoReturn

import pulumi
import requests
from pulumi import dynamic

from .api_client import StarburstAPIClient


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
                pulumi.log.error("No role_id returned from create_role")
                return pulumi.dynamic.CreateResult(id_="", outs={})

            # Get current privileges from Starburst (for logging/verification)
            _ = api_client.get_role_privileges(role_id)

            # Compare old and new privileges
            old_privileges = props.get("privileges", [])
            new_privileges = props.get("privileges", [])

            # Identify privileges to revoke and grant
            old_priv_set = {self._privilege_key(p) for p in old_privileges}
            new_priv_set = {self._privilege_key(p) for p in new_privileges}

            privileges_to_revoke = [
                p
                for p in old_privileges
                if self._privilege_key(p) in (old_priv_set - new_priv_set)
            ]
            privileges_to_grant = [
                p
                for p in new_privileges
                if self._privilege_key(p) in (new_priv_set - old_priv_set)
            ]

            pulumi.log.info(
                f"Role {props['role_name']}: "
                f"{len(privileges_to_revoke)} to revoke, "
                f"{len(privileges_to_grant)} to grant"
            )

            # Revoke old privileges
            for privilege in privileges_to_revoke:
                try:
                    api_client.revoke_privilege(role_id, privilege)
                    pulumi.log.info(
                        f"Revoked {privilege.get('privilege')} from role {role_id}"
                    )
                except requests.RequestException as e:
                    pulumi.log.warn(
                        f"Failed to revoke privilege {privilege.get('privilege')}: {e}"
                    )

            # Grant new privileges
            for privilege in privileges_to_grant:
                try:
                    api_client.grant_privilege(role_id, privilege)
                    pulumi.log.info(
                        f"Granted {privilege.get('privilege')} to role {role_id}"
                    )
                except requests.RequestException as e:
                    pulumi.log.warn(
                        f"Failed to grant privilege {privilege.get('privilege')}: {e}"
                    )

            # Update description if changed
            if props.get("description"):
                description = props.get("description", "")
                api_client.update_role_description(role_id, description)

            return dynamic.CreateResult(
                id_=role_id,
                outs={
                    # Add connection info so delete can access it
                    "domain": props["domain"],
                    "client_id": props["client_id"],
                    "client_secret": props["client_secret"],
                    # Role info
                    "role_id": role_id,
                    "role_name": props["role_name"],
                    "description": props.get("description", ""),
                    "privileges": props.get("privileges", []),
                    "privileges_count": len(privileges_to_grant),
                    "status": "created",
                },
            )

        except (requests.RequestException, ValueError, KeyError) as e:
            msg = f"Failed to create role {props['role_name']}: {e}"
            raise RuntimeError(msg) from e

    def update(
        self, role_id: str, old_props: dict[str, Any], new_props: dict[str, Any]
    ) -> dynamic.UpdateResult:
        """Update a Starburst role's privileges."""
        try:
            api_client = StarburstAPIClient(
                domain=new_props["domain"],
                client_id=new_props["client_id"],
                client_secret=new_props["client_secret"],
            )

            # Get current privileges from Starburst
            _ = api_client.get_role_privileges(role_id)

            # Compare old and new privileges
            old_privileges = old_props.get("privileges", [])
            new_privileges = new_props.get("privileges", [])

            # Identify privileges to revoke and grant
            old_priv_set = {self._privilege_key(p) for p in old_privileges}
            new_priv_set = {self._privilege_key(p) for p in new_privileges}

            privileges_to_revoke = [
                p
                for p in old_privileges
                if self._privilege_key(p) in (old_priv_set - new_priv_set)
            ]
            privileges_to_grant = [
                p
                for p in new_privileges
                if self._privilege_key(p) in (new_priv_set - old_priv_set)
            ]

            pulumi.log.info(
                f"Role {new_props['role_name']}: "
                f"{len(privileges_to_revoke)} to revoke, "
                f"{len(privileges_to_grant)} to grant"
            )

            # Revoke old privileges
            for privilege in privileges_to_revoke:
                try:
                    api_client.revoke_privilege(role_id, privilege)
                    pulumi.log.info(
                        f"Revoked {privilege.get('privilege')} from role {role_id}"
                    )
                except requests.RequestException as e:
                    pulumi.log.warn(
                        f"Failed to revoke privilege {privilege.get('privilege')}: {e}"
                    )

            # Grant new privileges
            for privilege in privileges_to_grant:
                try:
                    api_client.grant_privilege(role_id, privilege)
                    pulumi.log.info(
                        f"Granted {privilege.get('privilege')} to role {role_id}"
                    )
                except requests.RequestException as e:
                    pulumi.log.warn(
                        f"Failed to grant privilege {privilege.get('privilege')}: {e}"
                    )

            # Update description if changed
            if old_props.get("description") != new_props.get("description"):
                api_client.update_role_description(
                    role_id, new_props.get("description", "")
                )

            return dynamic.UpdateResult(
                outs={
                    # Add connection info so delete can access it
                    "domain": new_props["domain"],
                    "client_id": new_props["client_id"],
                    "client_secret": new_props["client_secret"],
                    # Role info
                    "role_id": role_id,
                    "role_name": new_props["role_name"],
                    "description": new_props.get("description", ""),
                    "privileges": new_privileges,
                    "privileges_count": len(new_privileges),
                    "status": "updated",
                }
            )

        except (requests.RequestException, ValueError, KeyError) as e:
            msg = f"Failed to update role {new_props['role_name']}: {e}"
            raise RuntimeError(msg) from e

    def _privilege_key(self, privilege: dict[str, Any]) -> str:
        """Create a unique key for a privilege for comparison."""
        key_parts = [
            privilege.get("entity_kind", ""),
            privilege.get("privilege", ""),
            privilege.get("entity_id", ""),
            privilege.get("catalog_name", ""),
            privilege.get("schema_name", ""),
            privilege.get("table_name", ""),
        ]
        return "|".join(str(p) for p in key_parts if p)

    def delete(self, role_id: str, props: dict[str, Any]) -> None:
        """Delete a Starburst role."""
        try:
            pulumi.log.info(
                "Delete called for role %s with props keys: %s",
                role_id,
                list(props.keys()),
            )

            # Check if we have the required connection properties
            domain = props.get("domain")
            client_id = props.get("client_id")
            client_secret = props.get("client_secret")

            if not domain:
                pulumi.log.error(
                    f"Missing 'domain' property for role {role_id} deletion. "
                    f"Available props: {list(props.keys())}. "
                    f"The role may need to be deleted manually in Starburst UI, "
                    f"then removed from Pulumi state using: "
                    f"pulumi state delete <urn>"
                )
                # Don't raise an error, just warn and continue
                # This allows the resource to be removed from state
                return

            if not all([client_id, client_secret]):
                pulumi.log.error(
                    f"Missing credentials for role {role_id} deletion. "
                    f"The role may need to be deleted manually in Starburst UI."
                )
                return

            # Type narrowing for mypy - these should always be strings at this point
            if not isinstance(domain, str):
                pulumi.log.error(
                    f"Invalid domain type for role {role_id} deletion: {type(domain)}"
                )
                return
            if not isinstance(client_id, str):
                pulumi.log.error(
                    f"Invalid client_id type for role {role_id} deletion: "
                    f"{type(client_id)}"
                )
                return
            if not isinstance(client_secret, str):
                pulumi.log.error(
                    f"Invalid client_secret type for role {role_id} deletion: "
                    f"{type(client_secret)}"
                )
                return

            api_client = StarburstAPIClient(
                domain=domain,
                client_id=client_id,
                client_secret=client_secret,
            )

            success = api_client.delete_role(role_id)
            if not success:
                pulumi.log.warn(
                    f"Failed to delete role {role_id}, it may have already been deleted"
                )

        except requests.RequestException as e:
            pulumi.log.warn(f"Error during role deletion {role_id}: {e}")
        except (ValueError, KeyError, TypeError) as e:
            pulumi.log.error(
                f"Unexpected error during role deletion {role_id}: {e}. "
                "The role may still exist in Starburst."
            )
            # Don't re-raise - allow the resource to be removed from state


class StarburstRole(dynamic.Resource):
    """A Starburst role resource."""

    def __init__(
        self,
        name: str,
        props: dict[str, Any],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(StarburstRoleProvider(), name, props, opts)
