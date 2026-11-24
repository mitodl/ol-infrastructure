"""Pulumi dynamic provider for a realm's scim-for-keycloak inbound SCIM server.

The plugin serves SCIM for each realm at ``/realms/<realm>/scim/v2`` (what
mitol.scim's ``scim_sync`` calls), configured by that realm's
``KeycloakServiceProviderConfig`` in the plugin's own tables. A realm has one,
and until it is saved the admin backend lists a default with no ``id`` and
``active: false``; the server rejects requests while it is inactive.

The server also rejects a token from any client not in the config's
``authorizedClients`` ("Client '...' is not authorized to access the SCIM
endpoints"), whatever ``limitClientAccess`` says: that flag only restricts which
users and groups an authorized client may change. Each entry needs the client's
internal id as well as its clientId, and the plugin lists both for every client
in the realm under ``KeycloakRealmInfos``, so clients are declared by clientId
and resolved from that listing.

The backend is reached under the master realm, as for remote providers (see
remote_provider.py): the master listing returns the config of every realm the
caller holds ``<realm>-realm`` ``scim-admin`` on, a POST there saves the config
for the realm named in its body, and a PUT to its ``id`` updates it. Writes are
read-modify-write, overlaying the declared attributes on the live (or default)
config. Verified on plugin 4.1.5. The plugin answers DELETE with 501, so
deleting the Pulumi resource leaves the config as it is.
"""

from collections.abc import Sequence
from typing import Any

import pulumi
import requests
from pulumi import dynamic
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ol_infrastructure.providers.http import raise_for_status_with_body
from ol_infrastructure.providers.keycloak_scim.remote_provider import (
    REQUEST_TIMEOUT_SECONDS,
    _backend,
    _list,
    _overlay,
    _project,
    _restore_ints,
    _session,
)

SERVICE_PROVIDER_CONFIG = "KeycloakServiceProviderConfig"
SERVICE_PROVIDER_CONFIG_SCHEMA = (
    "urn:goldfish:params:scim:schemas:core:2.0:KeycloakServiceProviderConfig"
)
REALM_INFOS_SCHEMA = "urn:goldfish:params:scim:schemas:core:2.0:KeycloakRealmInfos"
# Read-only parts of a listed config: the realm's clients and roles, offered to
# the admin console for the access settings, and the resource metadata.
READ_ONLY_ATTRIBUTES = ("meta", REALM_INFOS_SCHEMA)


class ScimServiceProviderSpec(BaseModel):
    """Declared attributes of a realm's inbound SCIM server.

    Only these attributes are managed; every other one keeps its live value.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    realm: str
    active: bool

    def to_scim(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, exclude={"realm"})


def _endpoint(props: dict[str, Any]) -> str:
    return _backend(props, SERVICE_PROVIDER_CONFIG)


def _live(session: requests.Session, props: dict[str, Any]) -> dict[str, Any] | None:
    matches = [
        config
        for config in _list(session, props, SERVICE_PROVIDER_CONFIG)
        if config["realm"]["name"] == props["realm"]
    ]
    return matches[0] if matches else None


def _authorized_clients(
    live: dict[str, Any], client_ids: list[str]
) -> list[dict[str, str]]:
    available = {
        client["clientId"]: client["id"]
        for client in live.get(REALM_INFOS_SCHEMA, {}).get("availableClients", [])
    }
    missing = sorted(set(client_ids) - set(available))
    if missing:
        msg = f"Clients not found in realm {live['realm']['name']}: {missing}"
        raise RuntimeError(msg)
    return [
        {"id": available[client_id], "clientId": client_id} for client_id in client_ids
    ]


def _save(session: requests.Session, props: dict[str, Any]) -> dict[str, Any]:
    live = _live(session, props)
    if live is None:
        msg = (
            f"No SCIM service provider config listed for realm {props['realm']}: "
            "the realm does not exist, or the client lacks its scim-admin role"
        )
        raise RuntimeError(msg)
    body = _overlay(
        {key: value for key, value in live.items() if key not in READ_ONLY_ATTRIBUTES},
        _restore_ints(props["spec"]),
    )
    body["schemas"] = [SERVICE_PROVIDER_CONFIG_SCHEMA]
    if props.get("authorized_clients") is not None:
        body["authorizedClients"] = _authorized_clients(
            live, props["authorized_clients"]
        )
    if live.get("id"):
        response = session.put(
            f"{_endpoint(props)}/{live['id']}",
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    else:
        response = session.post(
            _endpoint(props), json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
    raise_for_status_with_body(response)
    return response.json()


def _sorted(client_ids: list[str] | None) -> list[str] | None:
    return None if client_ids is None else sorted(client_ids)


class ScimServiceProviderConfigProvider(dynamic.ResourceProvider):
    """Create/update/read for one realm's inbound SCIM server config."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        saved = _save(_session(props), props)
        return dynamic.CreateResult(id_=saved["id"], outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:  # noqa: ARG002
        # Looked up by realm, not id_: the realm has only one config.
        live = _live(_session(props), props)
        if live is None or not live.get("id"):
            return dynamic.ReadResult(id_="", outs={})
        outs = {**props, "spec": _project(live, props["spec"])}
        if props.get("authorized_clients") is not None:
            # An entry whose client was deleted and recreated keeps the old
            # internal id; dropping it here makes that show as drift.
            current = {
                client["clientId"]: client["id"]
                for client in live.get(REALM_INFOS_SCHEMA, {}).get(
                    "availableClients", []
                )
            }
            outs["authorized_clients"] = [
                client["clientId"]
                for client in live.get("authorizedClients", [])
                if current.get(client["clientId"]) == client["id"]
            ]
        return dynamic.ReadResult(id_=live["id"], outs=outs)

    def diff(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.DiffResult:
        replaces = ["realm"] if _olds.get("realm") != _news.get("realm") else []
        changed = [
            key
            for key in ("keycloak_url", "realm", "client_id", "client_secret", "spec")
            if _olds.get(key) != _news.get(key)
        ]
        # The plugin's order is not the declared order.
        if _sorted(_olds.get("authorized_clients")) != _sorted(
            _news.get("authorized_clients")
        ):
            changed.append("authorized_clients")
        return dynamic.DiffResult(changes=bool(changed), replaces=replaces)

    def update(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        _save(_session(_news), _news)
        return dynamic.UpdateResult(outs=_news)

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        pulumi.log.info(
            f"Leaving the SCIM service provider config of realm {props['realm']} "
            "in place: the plugin does not support deleting it"
        )


class ScimServiceProviderConfig(dynamic.Resource):
    """A realm's inbound SCIM server configuration in scim-for-keycloak.

    :param name: Pulumi resource name.
    :param keycloak_url: Keycloak base URL. Must match KC_HOSTNAME because the
        admin backend runs with ``scim-admin-url-check=no-context-path``.
    :param client_id: Master realm client whose service account holds
        ``master-realm`` ``scim-admin`` and the realm's ``scim-admin``.
    :param client_secret: Secret for ``client_id``.
    :param spec: Declared attributes of the config.
    :param authorized_clients: clientIds in the realm allowed to call its SCIM
        endpoints, replacing the live list. ``None`` leaves the list unmanaged.
        Passing a client resource's ``client_id`` orders this after the client.
    :param opts: Resource options.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        keycloak_url: pulumi.Input[str],
        client_id: pulumi.Input[str],
        client_secret: pulumi.Input[str],
        spec: ScimServiceProviderSpec,
        authorized_clients: Sequence[pulumi.Input[str]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            ScimServiceProviderConfigProvider(),
            name,
            {
                "keycloak_url": keycloak_url,
                "realm": spec.realm,
                "client_id": client_id,
                "client_secret": pulumi.Output.secret(client_secret),
                "spec": spec.to_scim(),
                "authorized_clients": (
                    None
                    if authorized_clients is None
                    else pulumi.Output.all(*authorized_clients)
                ),
            },
            pulumi.ResourceOptions.merge(
                opts,
                pulumi.ResourceOptions(additional_secret_outputs=["client_secret"]),
            ),
        )
