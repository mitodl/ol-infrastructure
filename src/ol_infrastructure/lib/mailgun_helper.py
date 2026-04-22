import httpx
import pulumi

MAILGUN_API_BASE_US = "https://api.mailgun.net"
MAILGUN_API_BASE_EU = "https://api.eu.mailgun.net"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
_REQUEST_TIMEOUT = 10.0  # seconds


def _api_base(region: str) -> str:
    return MAILGUN_API_BASE_EU if region == "eu" else MAILGUN_API_BASE_US


def mailgun_domain_opts(
    domain_name: str,
    api_key: str,
    *,
    managed: bool,
    region: str = "us",
) -> pulumi.ResourceOptions:
    """Return ResourceOptions for a Mailgun domain resource.

    If managed is True, the domain is already in Pulumi state and no import is needed.
    If managed is False, the Mailgun API is queried. If the domain exists, import opts
    are returned so Pulumi brings it into state on the next up. If the domain does not
    exist, empty opts are returned so Pulumi creates it.

    After a successful ``pulumi up`` that imports the domain, set ``managed: true`` in
    the stack config so subsequent runs skip the API call.

    :param domain_name: The Mailgun domain name, e.g. ``mail.learn.mit.edu``
    :param api_key: Mailgun API key
    :param managed: Whether the domain is already under Pulumi management
    :param region: Mailgun region, either ``"us"`` (default) or ``"eu"``

    :returns: ResourceOptions with import_ set if the domain exists and is not yet
        managed
    :rtype: pulumi.ResourceOptions
    """
    if managed:
        return pulumi.ResourceOptions()

    try:
        resp = httpx.get(
            f"{_api_base(region)}/v4/domains/{domain_name}",
            auth=("api", api_key),
            timeout=_REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        msg = f"Timed out querying Mailgun API for domain {domain_name!r}"
        raise RuntimeError(msg) from exc
    except httpx.RequestError as exc:
        msg = f"Error contacting Mailgun API for domain {domain_name!r}: {exc}"
        raise RuntimeError(msg) from exc

    if resp.status_code == HTTP_OK:
        return pulumi.ResourceOptions(import_=f"{region}:{domain_name}")
    if resp.status_code == HTTP_NOT_FOUND:
        return pulumi.ResourceOptions()
    msg = (
        f"Unexpected Mailgun API response for domain {domain_name!r}: "
        f"HTTP {resp.status_code}. Check that the API key is valid and has "
        f"sufficient permissions."
    )
    raise RuntimeError(msg)


def mailgun_credential_opts(
    domain_name: str,
    login: str,
    api_key: str,
    *,
    managed: bool,
    region: str = "us",
) -> pulumi.ResourceOptions:
    """Return ResourceOptions for a Mailgun DomainCredential resource.

    Mailgun's API does not expose SMTP passwords, so ``ignore_changes=["password"]``
    is always included in the returned opts. If managed is False, the credential list
    for the domain is fetched and import opts are returned if the login already exists.

    After a successful ``pulumi up`` that imports the credential, set ``managed: true``
    in the stack config so subsequent runs skip the API call.

    :param domain_name: The Mailgun domain name, e.g. ``mail.learn.mit.edu``
    :param login: The credential login without the domain suffix, e.g. ``no-reply``
    :param api_key: Mailgun API key
    :param managed: Whether the credential is already under Pulumi management
    :param region: Mailgun region, either ``"us"`` (default) or ``"eu"``

    :returns: ResourceOptions with import_ set if applicable; always includes
        ignore_changes=["password"]
    :rtype: pulumi.ResourceOptions
    """
    if managed:
        return pulumi.ResourceOptions(ignore_changes=["password"])

    try:
        resp = httpx.get(
            f"{_api_base(region)}/v3/{domain_name}/credentials",
            auth=("api", api_key),
            timeout=_REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        msg = f"Timed out querying Mailgun credentials API for domain {domain_name!r}"
        raise RuntimeError(msg) from exc
    except httpx.RequestError as exc:
        msg = (
            f"Error contacting Mailgun credentials API for domain "
            f"{domain_name!r}: {exc}"
        )
        raise RuntimeError(msg) from exc

    if resp.status_code == HTTP_NOT_FOUND:
        return pulumi.ResourceOptions(ignore_changes=["password"])
    if resp.status_code == HTTP_OK:
        logins = {item["login"] for item in resp.json().get("items", [])}
        if f"{login}@{domain_name}" in logins:
            return pulumi.ResourceOptions(
                import_=f"{region}:{login}@{domain_name}",
                ignore_changes=["password"],
            )
        return pulumi.ResourceOptions(ignore_changes=["password"])
    msg = (
        f"Unexpected Mailgun API response for credentials on domain "
        f"{domain_name!r} (login {login!r}): HTTP {resp.status_code}. "
        f"Check that the API key is valid and has sufficient permissions."
    )
    raise RuntimeError(msg)
