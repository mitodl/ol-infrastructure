"""Pulumi management of the resources that live inside a single GCP project.

An :class:`OLGCPProject` owns the three GCP resource types that the credential
inventory in ``docs/plans/gcp-service-account-consumer-map.md`` found to be
both load-bearing and Pulumi-manageable:

* **enabled services** -- ``gcp.projects.Service``
* **service accounts and their project role bindings** -- ``gcp.serviceaccount``
* **API keys, with mandatory restrictions** -- ``gcp.projects.ApiKey``

It deliberately does *not* create the project itself. Project creation needs an
organization or folder parent plus a billing account, and where OL's projects
are allowed to sit is still an open question with IS&T. Adopting the resources
inside existing projects does not wait on that answer, so this component takes
the project id as configuration and leaves the project alone.

It also deliberately does not create service-account *keys*. Downloaded key
material is what this whole migration exists to remove; a workload that needs
to authenticate as a service account gets Workload Identity Federation, and a
credential that genuinely cannot is created by hand and recorded as an
exception.

Two GCP credential types are absent because no API can manage them:

* **Generic OAuth 2.0 clients** ("Sign in with Google") have no create, update
  or even *list* API. They are hand-created and stored in Vault/SOPS.
* **reCAPTCHA keys** are manageable (``gcp.recaptcha.EnterpriseKey``) but the
  site key is baked into deployed frontends, so rotating one is an application
  release rather than an infrastructure change. They get their own component
  once the app-side cutover is designed.
"""

from enum import StrEnum
from typing import Any

import pulumi_gcp as gcp
from pulumi import ComponentResource, Output, ResourceOptions
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ol_infrastructure.lib.ol_types import GCPBase


class APIKeyRestrictionType(StrEnum):
    """The restriction shapes an API key may carry.

    ``api_targets`` is orthogonal to the other three -- it limits *which APIs*
    a key may call, while the others limit *who* may call. A key needs at least
    one of these; six keys in the legacy estate carry none at all, including
    the only unrestricted key that is actually serving traffic.
    """

    android = "android_key_restrictions"
    api_targets = "api_targets"
    browser = "browser_key_restrictions"
    ios = "ios_key_restrictions"
    server = "server_key_restrictions"


class OLGCPServiceAccountConfig(BaseModel):
    """A service account and the project-level roles bound to it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: str
    display_name: str
    description: str = ""
    # Project-level roles. Resource-level grants (a specific bucket, a specific
    # dataset) belong on the resource, not here.
    project_roles: list[str] = Field(default_factory=list)
    # Set to the live resource id to adopt an existing account instead of
    # creating one. See docs/plans/gcp-pulumi-import-strategy.md for the id
    # format of each resource type.
    import_id: str | None = None


class OLGCPAPIKeyConfig(BaseModel):
    """An API key. Restrictions are required, not optional."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    key_name: str
    display_name: str
    restrictions: dict[str, Any]
    import_id: str | None = None

    @model_validator(mode="after")
    def enforce_restrictions(self) -> "OLGCPAPIKeyConfig":
        declared = {
            key
            for key, value in self.restrictions.items()
            if value not in (None, [], {})
        }
        valid = {member.value for member in APIKeyRestrictionType}
        if unknown := declared - valid:
            msg = f"Unknown API key restriction(s) {sorted(unknown)}. Valid: {sorted(valid)}"  # noqa: E501
            raise ValueError(msg)
        if not declared:
            msg = (
                f"API key {self.key_name} declares no restrictions. An "
                "unrestricted key is usable by anyone who obtains it against "
                "every API enabled on the project. Restrict it by caller "
                "(browser/server/android/ios) or by API (api_targets), or "
                "delete the key."
            )
            raise ValueError(msg)
        return self


class OLGCPProjectConfig(GCPBase):
    """Configuration for the resources managed inside one GCP project."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Service names as GCP knows them, e.g. "youtube.googleapis.com".
    enabled_services: list[str] = Field(default_factory=list)
    service_accounts: list[OLGCPServiceAccountConfig] = Field(default_factory=list)
    api_keys: list[OLGCPAPIKeyConfig] = Field(default_factory=list)


class OLGCPProject(ComponentResource):
    """Adopt and manage the contents of an existing GCP project."""

    def __init__(
        self,
        name: str,
        config: OLGCPProjectConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__("ol:infrastructure:gcp:Project", name, None, opts)
        child_opts = ResourceOptions(parent=self).merge(opts)

        self.project_id = config.project_id
        self.services: dict[str, gcp.projects.Service] = {}
        self.service_accounts: dict[str, gcp.serviceaccount.Account] = {}
        self.service_account_emails: dict[str, Output[str]] = {}
        self.api_keys: dict[str, gcp.projects.ApiKey] = {}

        for service in config.enabled_services:
            # disable_on_destroy is set explicitly rather than left to the
            # provider default: removing a service from this stack must never
            # turn the API off underneath whatever else is calling it. Nothing
            # in the legacy estate is well enough understood for that to be
            # safe, and several projects have consumers that emit no GCP-side
            # usage signal at all (reCAPTCHA classic, GCS reads).
            self.services[service] = gcp.projects.Service(
                f"{name}-service-{service.replace('.', '-')}",
                project=config.project_id,
                service=service,
                disable_on_destroy=False,
                disable_dependent_services=False,
                opts=child_opts,
            )

        for account in config.service_accounts:
            account_opts = _adoption_opts(child_opts, account.import_id)
            service_account = gcp.serviceaccount.Account(
                f"{name}-service-account-{account.account_id}",
                project=config.project_id,
                account_id=account.account_id,
                display_name=account.display_name,
                description=account.description,
                opts=account_opts,
            )
            self.service_accounts[account.account_id] = service_account
            self.service_account_emails[account.account_id] = service_account.email
            for role in account.project_roles:
                role_slug = role.replace("/", "-").replace(".", "-")
                gcp.projects.IAMMember(
                    f"{name}-{account.account_id}-{role_slug}",
                    project=config.project_id,
                    role=role,
                    member=service_account.email.apply(
                        lambda email: f"serviceAccount:{email}"
                    ),
                    opts=child_opts,
                )

        for api_key in config.api_keys:
            self.api_keys[api_key.key_name] = gcp.projects.ApiKey(
                f"{name}-api-key-{api_key.key_name}",
                project=config.project_id,
                display_name=api_key.display_name,
                restrictions=gcp.projects.ApiKeyRestrictionsArgs(
                    **api_key.restrictions
                ),
                opts=_adoption_opts(child_opts, api_key.import_id),
            )

        self.register_outputs(
            {
                "project_id": config.project_id,
                "service_account_emails": self.service_account_emails,
            }
        )


def _adoption_opts(opts: ResourceOptions, import_id: str | None) -> ResourceOptions:
    """Add import and protection options for a resource being adopted.

    ``protect`` travels with ``import_`` on purpose. An adopted resource is one
    that predates this stack and has consumers the stack does not know about,
    so a diff that resolves to a replacement -- which for a service account or
    an API key means the credential value changes -- has to fail loudly rather
    than proceed. Drop the protection deliberately, per resource, once the
    consumers are known.
    """
    if import_id is None:
        return opts
    return opts.merge(ResourceOptions(import_=import_id, protect=True))


__all__ = [
    "APIKeyRestrictionType",
    "OLGCPAPIKeyConfig",
    "OLGCPProject",
    "OLGCPProjectConfig",
    "OLGCPServiceAccountConfig",
]
