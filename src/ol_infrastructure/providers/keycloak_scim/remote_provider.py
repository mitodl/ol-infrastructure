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
attributes present in ``spec`` are overlaid before the PUT. A SCIM PUT replaces
the whole resource, so sending the declared attributes alone would reset
everything else (filters, truststore, attribute mappings) to plugin defaults.
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

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

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
    (``retry_interval_in_seconds`` -> ``retryIntervalInSeconds``).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

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
        """Render the managed attributes as a SCIM resource fragment."""
        return self.model_dump(by_alias=True, exclude_none=True)


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


def _find_by_name(
    session: requests.Session, props: dict[str, Any]
) -> dict[str, Any] | None:
    response = session.get(
        _endpoint(props),
        params={"count": LIST_PAGE_SIZE},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    _raise_for_status(response)
    name = props["spec"]["name"]
    matches = [r for r in response.json().get("Resources", []) if r["name"] == name]
    if len(matches) > 1:
        msg = f"{len(matches)} remote SCIM providers are named {name!r}"
        raise RuntimeError(msg)
    return matches[0] if matches else None


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
    """Reduce a live resource to the shape of the declared spec.

    Keys the backend does not return (e.g. masked secrets) keep their declared
    value, so drift on them cannot be detected.
    """
    if isinstance(live, dict) and isinstance(declared, dict):
        return {
            key: _project(live[key], value) if key in live else value
            for key, value in declared.items()
        }
    if isinstance(live, list) and isinstance(declared, list):
        return [
            _project(live[index], value) if index < len(live) else value
            for index, value in enumerate(declared)
        ]
    return live


def _put(
    session: requests.Session, props: dict[str, Any], live: dict[str, Any]
) -> dict[str, Any]:
    body = _overlay(
        {key: value for key, value in live.items() if key != "meta"}, props["spec"]
    )
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
                json={"schemas": [SCIM_REMOTE_PROVIDER_SCHEMA], **props["spec"]},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            _raise_for_status(response)
            created = response.json()
        return dynamic.CreateResult(id_=created["id"], outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        session = _session(props)
        response = session.get(
            f"{_endpoint(props)}/{id_}", timeout=REQUEST_TIMEOUT_SECONDS
        )
        _raise_for_status(response)
        return dynamic.ReadResult(
            id_=id_,
            outs={**props, "spec": _project(response.json(), props["spec"])},
        )

    def diff(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.DiffResult:
        replaces = [
            key for key in ("keycloak_url", "realm") if _olds.get(key) != _news[key]
        ]
        changed = [
            key
            for key in ("keycloak_url", "realm", "client_id", "client_secret", "spec")
            if _olds.get(key) != _news[key]
        ]
        return dynamic.DiffResult(
            changes=bool(changed),
            replaces=replaces,
            delete_before_replace=bool(replaces),
        )

    def update(
        self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]
    ) -> dynamic.UpdateResult:
        session = _session(_news)
        response = session.get(
            f"{_endpoint(_news)}/{_id}", timeout=REQUEST_TIMEOUT_SECONDS
        )
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

    :param name: Pulumi resource name.
    :param keycloak_url: Keycloak base URL. Must match KC_HOSTNAME because the
        admin backend runs with ``scim-admin-url-check=no-context-path``.
    :param realm: Realm the remote provider belongs to.
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
        realm: pulumi.Input[str],
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
                "realm": realm,
                "client_id": client_id,
                "client_secret": pulumi.Output.secret(client_secret),
                "spec": pulumi.Output.secret(spec.to_scim()),
            },
            pulumi.ResourceOptions.merge(
                opts,
                pulumi.ResourceOptions(
                    additional_secret_outputs=["client_secret", "spec"]
                ),
            ),
        )
