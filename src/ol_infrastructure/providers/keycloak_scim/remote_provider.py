"""Pulumi dynamic provider for scim-for-keycloak remote SCIM providers.

The scim-for-keycloak enterprise plugin stores its outbound provisioning targets
in its own JPA tables, not in Keycloak's component model, so pulumi_keycloak
cannot manage them. They are only reachable through the plugin's SCIM admin
backend at ``/realms/master/scim/admin/backend/scim/v2/RemoteScimProviderConfig``.

Since plugin 4.x the backend accepts client_credentials tokens from a master
realm service account that holds the ``master-realm`` client role
``scim-admin``. Earlier releases only accepted ``admin-cli`` and
``security-admin-console`` tokens.

Requests go to the backend under the master realm, whatever realm the provider
belongs to. The plugin verifies the token in its issuer realm but then looks up
the token's client in the realm from the URL, and the service account client
only exists in master, so ``/realms/olapps/...`` returns 401. The list endpoint
under master returns providers from every realm the caller holds
``<realm>-realm`` ``scim-admin`` on.

Writes are read-modify-write: the live resource is fetched and only the
declared attributes are overlaid before the PUT. A SCIM PUT replaces the whole
resource, so sending the declared attributes alone would reset everything else
(filters, truststore, attribute mappings) to plugin defaults.
"""

from typing import Any, Literal

import pulumi
import requests
from pulumi import dynamic
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel

from ol_infrastructure.providers.http import raise_for_status_with_body

SCIM_REMOTE_PROVIDER_SCHEMA = (
    "urn:goldfish:params:scim:schemas:core:2.0:ScimRemoteProvider"
)
SCIM_SCHEMA_ATTRIBUTE_SCHEMA = (
    "urn:goldfish:params:scim:schemas:core:2.0:KeycloakResourceSchemaAttribute"
)
SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_CONTENT_TYPE = "application/scim+json"
REQUEST_TIMEOUT_SECONDS = 30
ADMIN_REALM = "master"
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


class ScimUserAttribute(BaseModel):
    """A custom attribute on the provider's User schema.

    The plugin sends a Keycloak user attribute to the target only when the
    provider's User schema has an attribute for it; the RFC 7643 default schema
    has none of ours. ``custom_attribute_name`` is the Keycloak user attribute
    it reads, ``name`` the SCIM attribute it sends (e.g. ``fullName``, which
    mitol-django-scim targets read for the user's name).
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    name: str
    type: Literal["string", "integer", "boolean", "decimal", "dateTime"]
    custom_attribute_name: str


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
    # What the plugin believes the target supports, and so what it sends: with
    # patch_supported false it issues PUTs instead of PATCHes. Only the
    # reloadConfiguration command reads the target's /ServiceProviderConfig
    # (and overwrites these); loading the default configuration resets them to
    # RFC defaults. A provider created without them is less capable than the
    # live ones, which were configured through its admin console.
    filter_supported: bool | None = None
    filter_max_results: int | None = None
    sort_supported: bool | None = None
    patch_supported: bool | None = None
    change_password_supported: bool | None = None
    bulk_supported: bool | None = None
    bulk_max_operations: int | None = None
    bulk_max_payload_size: int | None = None
    etag_supported: bool | None = None
    authentication_list: list[ScimAuthentication] | None = None
    # Created or corrected when declared; undeclared attributes on the live
    # schema are left alone, as with every other attribute here.
    user_attributes: list[ScimUserAttribute] | None = None

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
                exclude={"realm", "authentication_list", "user_attributes"},
            ),
            "assignedRealm": {"realmName": self.realm},
        }

    def user_attributes_to_scim(self) -> list[dict[str, Any]] | None:
        """Render the declared User-schema attributes."""
        if self.user_attributes is None:
            return None
        return [
            attribute.model_dump(by_alias=True) for attribute in self.user_attributes
        ]

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
        f"{props['keycloak_url']}/realms/{ADMIN_REALM}/protocol/openid-connect/token",
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


def _backend(props: dict[str, Any], resource: str) -> str:
    return (
        f"{props['keycloak_url']}/realms/{ADMIN_REALM}"
        f"/scim/admin/backend/scim/v2/{resource}"
    )


def _endpoint(props: dict[str, Any]) -> str:
    return _backend(props, "RemoteScimProviderConfig")


def _list(
    session: requests.Session, props: dict[str, Any], resource: str
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    total_results = 1
    while len(resources) < total_results:
        response = session.get(
            _backend(props, resource),
            params={"count": LIST_PAGE_SIZE, "startIndex": len(resources) + 1},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        raise_for_status_with_body(response)
        page = response.json()
        page_resources = page.get("Resources", [])
        if not page_resources:
            break
        resources.extend(page_resources)
        total_results = page["totalResults"]
    return resources


def _restore_ints(value: Any) -> Any:
    """Turn integral floats back into ints.

    Dynamic provider inputs cross the engine as a protobuf ``Struct``, which
    only has doubles, so ``300`` arrives as ``300.0``. The plugin rejects a
    float for its integer attributes (e.g. ``retryIntervalInSeconds``), and the
    spec declares no float attributes.
    """
    if isinstance(value, dict):
        return {key: _restore_ints(entry) for key, entry in value.items()}
    if isinstance(value, list):
        return [_restore_ints(entry) for entry in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _declared(props: dict[str, Any]) -> dict[str, Any]:
    declared = dict(props["spec"])
    if props["authentication"] is not None:
        declared["authenticationList"] = props["authentication"]
    return _restore_ints(declared)


def _find_by_name(
    session: requests.Session, props: dict[str, Any]
) -> dict[str, Any] | None:
    # The list endpoint returns providers from every realm the caller is
    # authorized for, and names are not unique, so match on realm as well.
    name = props["spec"]["name"]
    matches = [
        resource
        for resource in _list(session, props, "RemoteScimProviderConfig")
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
    session: requests.Session,
    props: dict[str, Any],
    live: dict[str, Any],
    commands: dict[str, bool] | None = None,
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
        json={**body, **(commands or {})},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    raise_for_status_with_body(response)
    return response.json()


def _load_default_configuration(
    session: requests.Session, props: dict[str, Any], created: dict[str, Any]
) -> None:
    """Give a new provider the RFC 7643 User and Group resource types.

    A provider created through the admin backend has no resource types, and
    the plugin skips one without them ("no resource-assignments for this
    provider"), so nothing is provisioned. The command is ignored on POST and
    only acts on PUT. reloadConfiguration would import the target's own
    ResourceTypes instead, but mitol-django-scim advertises their endpoints as
    /scim/v2/Users, which the plugin appends to base_url a second time.

    Loading defaults deletes the provider's existing schema configuration and
    resets the capability flags to RFC defaults, so it runs only on a provider
    this resource just created, and the declared attributes are put back after.
    """
    loaded = _put(session, props, created, {"useDefaultScimConfiguration": True})
    _put(session, props, loaded)


def _ensure_user_attributes(
    session: requests.Session, props: dict[str, Any], provider_id: str
) -> None:
    """Create or correct the declared attributes on the provider's User schema.

    Writing a schema attribute also makes the running plugin reload its cached
    provider configuration (observed on 4.1.5); a provider PUT alone does not.
    """
    declared = props.get("user_attributes")
    if not declared:
        return
    schema = next(
        (
            schema
            for schema in _list(session, props, "KeycloakResourceSchemas")
            if schema.get("scimRemoteProviderId") == provider_id
            and schema["schemaId"] == SCIM_USER_SCHEMA
        ),
        None,
    )
    if schema is None:
        msg = (
            f"Remote SCIM provider {props['spec']['name']!r} has no User schema; "
            "its SCIM configuration was never loaded"
        )
        raise RuntimeError(msg)
    live = {
        attribute["name"]: attribute
        for attribute in _list(session, props, "KeycloakResourceSchemaAttributes")
        if attribute["schemaId"] == schema["id"]
        and not attribute.get("parentAttribute")
    }
    for attribute in declared:
        existing = live.get(attribute["name"])
        if existing is None:
            response = session.post(
                _backend(props, "KeycloakResourceSchemaAttributes"),
                json={
                    "schemas": [SCIM_SCHEMA_ATTRIBUTE_SCHEMA],
                    "schemaId": schema["id"],
                    "mutability": "readWrite",
                    "returned": "default",
                    "uniqueness": "none",
                    "multiValued": False,
                    "required": False,
                    "caseExact": False,
                    **attribute,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        elif any(existing.get(key) != value for key, value in attribute.items()):
            response = session.put(
                f"{_backend(props, 'KeycloakResourceSchemaAttributes')}"
                f"/{existing['id']}",
                json={
                    **{key: value for key, value in existing.items() if key != "meta"},
                    **attribute,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        else:
            continue
        raise_for_status_with_body(response)


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
            raise_for_status_with_body(response)
            created = response.json()
            _load_default_configuration(session, props, created)
        _ensure_user_attributes(session, props, created["id"])
        return dynamic.CreateResult(id_=created["id"], outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        session = _session(props)
        response = _get(session, props, id_)
        if response.status_code == requests.codes.not_found:
            # An empty id tells the engine the resource is gone, so a provider
            # deleted in the admin UI drops out of state on refresh.
            return dynamic.ReadResult(id_="", outs={})
        raise_for_status_with_body(response)
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
                "user_attributes",
            )
            if _olds.get(key) != _news.get(key)
        ]
        return dynamic.DiffResult(changes=bool(changed))

    def update(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        session = _session(_news)
        response = _get(session, _news, _id)
        raise_for_status_with_body(response)
        _put(session, _news, response.json())
        _ensure_user_attributes(session, _news, _id)
        return dynamic.UpdateResult(outs=_news)

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        session = _session(_props)
        response = session.delete(
            f"{_endpoint(_props)}/{_id}", timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code == requests.codes.not_found:
            return
        raise_for_status_with_body(response)


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
                "user_attributes": spec.user_attributes_to_scim(),
            },
            pulumi.ResourceOptions.merge(
                opts,
                pulumi.ResourceOptions(
                    additional_secret_outputs=["client_secret", "authentication"]
                ),
            ),
        )
