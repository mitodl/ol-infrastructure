"""Pulumi dynamic provider for scim-for-keycloak remote SCIM providers.

The scim-for-keycloak enterprise plugin stores its outbound provisioning targets
in its own JPA tables, not in Keycloak's component model, so pulumi_keycloak
cannot manage them. They are only reachable through the plugin's SCIM admin
backend at ``/realms/{realm}/scim/admin/backend/scim/v2/RemoteScimProviderConfig``.

Since plugin 4.x the backend accepts client_credentials tokens from a master
realm service account that holds the ``master-realm`` client role
``scim-admin``. Earlier releases only accepted ``admin-cli`` and
``security-admin-console`` tokens.

Writes are read-modify-write: the live resource is fetched and only the
declared attributes are overlaid before the PUT. A SCIM PUT replaces the whole
resource, so sending the declared attributes alone would reset everything else
(filters, truststore, attribute mappings) to plugin defaults.
"""

from typing import Any

import pulumi
import requests
from pulumi import dynamic
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel

SCIM_REMOTE_PROVIDER_SCHEMA = (
    "urn:goldfish:params:scim:schemas:core:2.0:ScimRemoteProvider"
)
SCIM_CONTENT_TYPE = "application/scim+json"
REQUEST_TIMEOUT_SECONDS = 30
LIST_PAGE_SIZE = 100


class ScimAuthentication(BaseModel):
    """One entry of a remote provider's ``authenticationList``."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    authentication_type: str
    api_key_header_name: str | None = None
    authentication_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_endpoint: str | None = None
    scope: str | None = None


class ScimRemoteProviderSpec(BaseModel):
    """Declared attributes of a remote SCIM provider.

    Attributes left as ``None`` are not managed and keep their live value.
    Field names map to the plugin schema by camelCase alias
    (``retry_interval_in_seconds`` -> ``retryIntervalInSeconds``). Unknown keys
    are rejected so a misspelled attribute cannot be silently dropped.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    realm: str
    name: str
    base_url: str
    enabled: bool
    request_timeout: int | None = None
    connection_timeout: int | None = None
    socket_timeout: int | None = None
    hostname_verifier_on: bool | None = None
    max_patch_operations: int | None = None
    send_local_id_in_request: bool | None = None
    send_external_id_as_id_in_request: bool | None = None
    use_lower_case_in_filter_comparators: bool | None = None
    replay_on_failure: bool | None = None
    retry_interval_in_seconds: int | None = None
    retry_max_attempts: int | None = None
    authentication_list: list[ScimAuthentication] | None = None

    @field_validator("base_url")
    @classmethod
    def base_url_is_scim_root(cls, base_url: str) -> str:
        if not base_url.rstrip("/").endswith("/scim/v2"):
            msg = f"base_url must end with /scim/v2, got {base_url}"
            raise ValueError(msg)
        return base_url.rstrip("/")

    def to_scim(self) -> dict[str, Any]:
        """Render the non-credential attributes as a SCIM resource fragment.

        The plugin binds a provider to a realm only through ``assignedRealm`` in
        the request body; the realm in the URL is ignored, and an unbound
        provider is never loaded for provisioning.
        """
        return {
            **self.model_dump(
                by_alias=True,
                exclude_none=True,
                exclude={"realm", "authentication_list"},
            ),
            "assignedRealm": {"realmName": self.realm},
        }

    def authentication_to_scim(self) -> list[dict[str, Any]] | None:
        """Render ``authenticationList``, which carries credentials."""
        if self.authentication_list is None:
            return None
        return [
            entry.model_dump(by_alias=True, exclude_none=True)
            for entry in self.authentication_list
        ]


def _session(props: dict[str, Any]) -> requests.Session:
    token_response = requests.post(
        f"{props['keycloak_url']}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": props["client_id"],
            "client_secret": props["client_secret"],
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    token_response.raise_for_status()
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token_response.json()['access_token']}",
            "Accept": SCIM_CONTENT_TYPE,
            "Content-Type": SCIM_CONTENT_TYPE,
        }
    )
    return session


def _endpoint(props: dict[str, Any]) -> str:
    return (
        f"{props['keycloak_url']}/realms/{props['realm']}"
        "/scim/admin/backend/scim/v2/RemoteScimProviderConfig"
    )


def _raise_for_status(response: requests.Response) -> None:
    # The SCIM error body carries the plugin's validation message, which
    # raise_for_status() alone would drop.
    if not response.ok:
        msg = (
            f"{response.request.method} {response.url} returned "
            f"{response.status_code}: {response.text}"
        )
        raise RuntimeError(msg)


def _declared(props: dict[str, Any]) -> dict[str, Any]:
    declared = dict(props["spec"])
    if props["authentication"] is not None:
        declared["authenticationList"] = props["authentication"]
    return declared


def _find_by_name(
    session: requests.Session, props: dict[str, Any]
) -> dict[str, Any] | None:
    # The list endpoint returns providers from every realm the caller is
    # authorized for, and names are not unique, so match on realm as well.
    resources: list[dict[str, Any]] = []
    total_results = 1
    while len(resources) < total_results:
        response = session.get(
            _endpoint(props),
            params={"count": LIST_PAGE_SIZE, "startIndex": len(resources) + 1},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status(response)
        page = response.json()
        page_resources = page.get("Resources", [])
        if not page_resources:
            break
        resources.extend(page_resources)
        total_results = page["totalResults"]
    name = props["spec"]["name"]
    matches = [
        resource
        for resource in resources
        if resource["name"] == name
        and (resource.get("assignedRealm") or {}).get("realmName") == props["realm"]
    ]
    if len(matches) > 1:
        msg = (
            f"{len(matches)} remote SCIM providers in realm {props['realm']} "
            f"are named {name!r}"
        )
        raise RuntimeError(msg)
    return matches[0] if matches else None


def _get(
    session: requests.Session, props: dict[str, Any], id_: str
) -> requests.Response:
    return session.get(f"{_endpoint(props)}/{id_}", timeout=REQUEST_TIMEOUT_SECONDS)


def _overlay(live: Any, declared: Any) -> Any:
    if isinstance(live, dict) and isinstance(declared, dict):
        return {
            **live,
            **{key: _overlay(live.get(key), value) for key, value in declared.items()},
        }
    if isinstance(live, list) and isinstance(declared, list):
        return [
            _overlay(live[index] if index < len(live) else None, value)
            for index, value in enumerate(declared)
        ]
    return declared


def _project(live: Any, declared: Any) -> Any:
    """Reduce a live resource to the shape of the declared attributes.

    Unmanaged (``None``) values stay unmanaged rather than pulling live values
    (and credentials) into state. Declared keys missing from the live resource
    are omitted, and list lengths follow the live side, so both show as drift.
    """
    if declared is None:
        return None
    if isinstance(live, dict) and isinstance(declared, dict):
        return {
            key: _project(live[key], value)
            for key, value in declared.items()
            if key in live
        }
    if isinstance(live, list) and isinstance(declared, list):
        return [
            _project(entry, declared[index]) if index < len(declared) else entry
            for index, entry in enumerate(live)
        ]
    return live


def _put(
    session: requests.Session, props: dict[str, Any], live: dict[str, Any]
) -> dict[str, Any]:
    body = _overlay(
        {key: value for key, value in live.items() if key != "meta"},
        _declared(props),
    )
    # Replace rather than merge: the plugin prefers realmId over realmName, so
    # a merged live realmId would win over the declared realm.
    body["assignedRealm"] = props["spec"]["assignedRealm"]
    body["schemas"] = [SCIM_REMOTE_PROVIDER_SCHEMA]
    response = session.put(
        f"{_endpoint(props)}/{live['id']}",
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    _raise_for_status(response)
    return response.json()


class ScimRemoteProviderProvider(dynamic.ResourceProvider):
    """CRUD for one scim-for-keycloak remote SCIM provider."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        session = _session(props)
        existing = _find_by_name(session, props)
        if existing:
            # Adopt providers that were configured by hand in the admin UI.
            pulumi.log.info(
                f"Adopting existing remote SCIM provider {existing['name']!r} "
                f"({existing['id']}) in realm {props['realm']}"
            )
            created = _put(session, props, existing)
        else:
            response = session.post(
                _endpoint(props),
                json={"schemas": [SCIM_REMOTE_PROVIDER_SCHEMA], **_declared(props)},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            _raise_for_status(response)
            created = response.json()
        return dynamic.CreateResult(id_=created["id"], outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        session = _session(props)
        response = _get(session, props, id_)
        if response.status_code == requests.codes.not_found:
            # An empty id tells the engine the resource is gone, so a provider
            # deleted in the admin UI drops out of state on refresh.
            return dynamic.ReadResult(id_="", outs={})
        _raise_for_status(response)
        live = response.json()
        return dynamic.ReadResult(
            id_=id_,
            outs={
                **props,
                "spec": _project(live, props["spec"]),
                "authentication": _project(
                    live.get("authenticationList"), props["authentication"]
                ),
            },
        )

    def diff(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.DiffResult:
        # realm is part of the resource name, so a realm change is already a
        # new resource; keycloak_url is only the connection endpoint.
        changed = [
            key
            for key in (
                "keycloak_url",
                "client_id",
                "client_secret",
                "spec",
                "authentication",
            )
            if _olds.get(key) != _news[key]
        ]
        return dynamic.DiffResult(changes=bool(changed))

    def update(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        session = _session(_news)
        response = _get(session, _news, _id)
        _raise_for_status(response)
        _put(session, _news, response.json())
        return dynamic.UpdateResult(outs=_news)

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        session = _session(_props)
        response = session.delete(
            f"{_endpoint(_props)}/{_id}", timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code == requests.codes.not_found:
            return
        _raise_for_status(response)


class ScimRemoteProvider(dynamic.Resource):
    """A remote SCIM provider (outbound provisioning target) in a Keycloak realm.

    Only ``authenticationList`` is marked secret, so changes to the other
    attributes stay readable in ``pulumi preview``.

    :param name: Pulumi resource name.
    :param keycloak_url: Keycloak base URL. Must match KC_HOSTNAME because the
        admin backend runs with ``scim-admin-url-check=no-context-path``.
    :param client_id: Master realm client whose service account holds
        ``master-realm`` ``scim-admin``.
    :param client_secret: Secret for ``client_id``.
    :param spec: Declared attributes of the remote provider.
    :param opts: Resource options.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        keycloak_url: pulumi.Input[str],
        client_id: pulumi.Input[str],
        client_secret: pulumi.Input[str],
        spec: ScimRemoteProviderSpec,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            ScimRemoteProviderProvider(),
            name,
            {
                "keycloak_url": keycloak_url,
                "realm": spec.realm,
                "client_id": client_id,
                "client_secret": pulumi.Output.secret(client_secret),
                "spec": spec.to_scim(),
                "authentication": pulumi.Output.secret(spec.authentication_to_scim()),
            },
            pulumi.ResourceOptions.merge(
                opts,
                pulumi.ResourceOptions(
                    additional_secret_outputs=["client_secret", "authentication"]
                ),
            ),
        )
