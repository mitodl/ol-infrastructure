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
