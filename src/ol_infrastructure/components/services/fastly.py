"""Component resource for creating and managing Fastly ServiceVCL configurations.

Encapsulates common patterns across MIT Open Learning applications:
HSTS headers, force-SSL, gzip/brotli compression, HTTPS/S3 logging,
TLS subscription management, and DNS record creation.
"""

import mimetypes
from typing import Any

import pulumi
import pulumi_fastly as fastly
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    ComponentResource,
    InvokeOptions,
    Output,
    ResourceOptions,
)
from pulumi_aws import route53
from pydantic import BaseModel, ConfigDict, Field

from bridge.lib.constants import FASTLY_A_TLS_1_3
from bridge.lib.magic_numbers import ONE_MEGABYTE_BYTE
from ol_infrastructure.lib.aws.route53_helper import (
    FIVE_MINUTES,
    fastly_certificate_validation_records,
    lookup_zone_id_from_domain,
)
from ol_infrastructure.lib.fastly import build_fastly_log_format_string

DEFAULT_GZIP_EXTENSIONS: set[str] = {
    ".css",
    ".csv",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".pdf",
    ".png",
    ".srt",
    ".svg",
    ".txt",
    ".vtt",
    ".xml",
}


def _build_gzip_settings(
    extensions: set[str],
) -> tuple[list[str], list[str]]:
    """Build sorted gzip extension and content-type lists from file extensions.

    Uses ``mimetypes.types_map`` to resolve content types for each extension.
    """
    gzip_extensions: set[str] = set()
    gzip_content_types: set[str] = set()
    for ext, content_type in mimetypes.types_map.items():
        if ext in extensions:
            gzip_extensions.add(ext.lstrip("."))
            gzip_content_types.add(content_type)
    return sorted(gzip_extensions), sorted(gzip_content_types)


class OLFastlyServiceVCLHTTPSLoggingConfig(BaseModel):
    """Configuration for Fastly HTTPS logging to a vector log proxy."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vector_log_proxy_domain: str | Output[str]
    encoded_credentials: str
    logging_name: str
    additional_static_fields: dict[str, str] = Field(default_factory=dict)


class OLFastlyServiceVCLS3LoggingConfig(BaseModel):
    """Configuration for Fastly S3 access logging."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bucket_name: str | Output[str]
    iam_role_arn: str | Output[str]
    path_prefix: str
    logging_name: str
    additional_static_fields: dict[str, str] = Field(default_factory=dict)
    gzip_level: int = 3


class OLFastlyServiceVCLTLSConfig(BaseModel):
    """TLS configuration for Fastly services.

    When ``managed`` is True, a ``TlsSubscription`` is created for automatic
    certificate provisioning with DNS validation via Route53.  When False,
    DNS records point at the static ``FASTLY_A_TLS_1_3`` IP addresses.
    """

    managed: bool = False
    certificate_authority: str = "certainly"


class OLFastlyServiceVCLDNSConfig(BaseModel):
    """DNS configuration for pointing a domain at Fastly.

    If ``zone_id`` is not provided it is resolved automatically via
    ``lookup_zone_id_from_domain``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    frontend_domain: str
    zone_id: str | Output[str] | None = None


class OLFastlyServiceVCLConfig(BaseModel):
    """Configuration for the :class:`OLFastlyServiceVCL` component resource.

    Required fields are ``service_name``, ``backends``, and ``domains``.
    All other fields have sensible defaults that match the most common usage
    across MIT Open Learning applications.

    To override the auto-generated force-SSL request setting, provide an
    explicit ``request_settings`` list.  To override the auto-generated gzip
    configuration, provide an explicit ``gzips`` list.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Required
    service_name: str
    backends: list[fastly.ServiceVclBackendArgs]
    domains: list[fastly.ServiceVclDomainArgs]

    # DNS — when set, a Route53 A record is created
    dns: OLFastlyServiceVCLDNSConfig | None = None

    # Common defaults
    enable_hsts: bool = True
    hsts_max_age: int = 300
    force_ssl: bool = True
    enable_gzip: bool = True
    gzip_file_extensions: set[str] | None = None
    gzips: list[fastly.ServiceVclGzipArgs] | None = None
    enable_brotli: bool = True
    enable_image_optimizer: bool = False
    image_optimizer_default_settings: (
        fastly.ServiceVclImageOptimizerDefaultSettingsArgs | None
    ) = None

    # TLS
    tls: OLFastlyServiceVCLTLSConfig = Field(
        default_factory=OLFastlyServiceVCLTLSConfig
    )

    # Logging
    https_logging: OLFastlyServiceVCLHTTPSLoggingConfig | None = None
    s3_logging: OLFastlyServiceVCLS3LoggingConfig | None = None

    # Custom VCL passthrough
    snippets: list[fastly.ServiceVclSnippetArgs] = Field(default_factory=list)
    conditions: list[fastly.ServiceVclConditionArgs] = Field(default_factory=list)
    cache_settings: list[fastly.ServiceVclCacheSettingArgs] = Field(
        default_factory=list
    )
    headers: list[fastly.ServiceVclHeaderArgs] = Field(default_factory=list)
    request_settings: list[fastly.ServiceVclRequestSettingArgs] | None = None
    response_objects: list[fastly.ServiceVclResponseObjectArgs] = Field(
        default_factory=list
    )
    dictionaries: list[fastly.ServiceVclDictionaryArgs] = Field(default_factory=list)

    # Other ServiceVcl options
    comment: str = "Managed by Pulumi"
    default_ttl: int | None = None
    stale_if_error: bool = False
    aliases: list[Alias] = Field(default_factory=list)
    service_vcl_resource_name: str | None = None
    """Original ``fastly.ServiceVcl`` resource name before it was wrapped in this
    component.  When set, an alias pointing to the old top-level resource is
    automatically added to the child ``ServiceVcl`` so Pulumi does not
    destroy-and-recreate the existing Fastly service on first deploy."""


def _build_headers(
    config: OLFastlyServiceVCLConfig,
) -> list[fastly.ServiceVclHeaderArgs]:
    """Return the merged list of caller-supplied and auto-generated headers."""
    all_headers = list(config.headers)
    if config.enable_hsts:
        all_headers.append(
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Strict-Transport-Security",
                name="Generated by force TLS and enable HSTS",
                source=f'"max-age={config.hsts_max_age}"',
                type="response",
            )
        )
    return all_headers


def _build_request_settings(
    config: OLFastlyServiceVCLConfig,
) -> list[fastly.ServiceVclRequestSettingArgs]:
    """Return explicit request settings or auto-generate a force-SSL default."""
    if config.request_settings is not None:
        return config.request_settings
    if config.force_ssl:
        return [
            fastly.ServiceVclRequestSettingArgs(
                force_ssl=True,
                name="Generated by force TLS and enable HSTS",
                xff="",
            )
        ]
    return []


def _build_gzips(
    config: OLFastlyServiceVCLConfig,
) -> list[fastly.ServiceVclGzipArgs]:
    """Return explicit gzip args, auto-generate from mimetypes, or an empty list."""
    if config.gzips is not None:
        return config.gzips
    if config.enable_gzip:
        extensions = config.gzip_file_extensions or DEFAULT_GZIP_EXTENSIONS
        sorted_ext, sorted_ct = _build_gzip_settings(extensions)
        return [
            fastly.ServiceVclGzipArgs(
                name="enable-gzip-compression",
                extensions=sorted_ext,
                content_types=sorted_ct,
            )
        ]
    return []


def _build_product_enablement(
    config: OLFastlyServiceVCLConfig,
) -> fastly.ServiceVclProductEnablementArgs | None:
    """Build product enablement args from boolean flags."""
    if not config.enable_brotli and not config.enable_image_optimizer:
        return None
    return fastly.ServiceVclProductEnablementArgs(
        brotli_compression=True if config.enable_brotli else None,
        image_optimizer=True if config.enable_image_optimizer else None,
    )


def _build_https_logging(
    config: OLFastlyServiceVCLConfig,
) -> list[fastly.ServiceVclLoggingHttpArgs]:
    """Build HTTPS logging args from the logging config, if present."""
    if not config.https_logging:
        return []
    log_cfg = config.https_logging
    return [
        fastly.ServiceVclLoggingHttpArgs(
            url=Output.all(domain=log_cfg.vector_log_proxy_domain).apply(
                lambda kwargs: f"https://{kwargs['domain']}/fastly"
            ),
            name=log_cfg.logging_name,
            content_type="application/json",
            format=build_fastly_log_format_string(
                additional_static_fields=log_cfg.additional_static_fields
            ),
            format_version=2,
            header_name="Authorization",
            header_value=f"Basic {log_cfg.encoded_credentials}",
            json_format="0",
            method="POST",
            request_max_bytes=ONE_MEGABYTE_BYTE,
        )
    ]


def _build_s3_logging(
    config: OLFastlyServiceVCLConfig,
) -> list[fastly.ServiceVclLoggingS3Args]:
    """Build S3 logging args from the logging config, if present."""
    if not config.s3_logging:
        return []
    s3_cfg = config.s3_logging
    return [
        fastly.ServiceVclLoggingS3Args(
            bucket_name=s3_cfg.bucket_name,
            name=s3_cfg.logging_name,
            format=build_fastly_log_format_string(
                additional_static_fields=s3_cfg.additional_static_fields
            ),
            gzip_level=s3_cfg.gzip_level,
            message_type="blank",
            path=s3_cfg.path_prefix,
            redundancy="standard",
            s3_iam_role=s3_cfg.iam_role_arn,
        )
    ]


def _normalize_alias(a: Alias) -> Alias:
    """Ensure an alias refers to a root-level resource.

    Aliases created with no explicit ``parent`` use ``Ellipsis`` as a
    sentinel, meaning Pulumi will inherit the resource's current parent
    (the component).  For migrations from standalone ``ServiceVcl``
    resources to this component, all aliases must point to the root
    stack (no parent).
    """
    if a.parent is ...:  # Ellipsis = "not specified by caller"
        return Alias(
            name=a.name,
            type_=a.type_,
            stack=a.stack,
            project=a.project,
            parent=ROOT_STACK_RESOURCE,
        )
    return a


class OLFastlyServiceVCL(ComponentResource):
    """Reusable component for Fastly ServiceVcl resources.

    Automatically handles the common patterns shared across MIT Open
    Learning applications:

    * HSTS response header
    * Force-SSL request setting
    * Gzip / Brotli compression
    * HTTPS and S3 logging to the vector log proxy
    * Managed TLS subscriptions with DNS-01 validation
    * Route53 DNS A records

    Attributes:
        service: The Fastly ``ServiceVcl`` resource.
        dns_record: The Route53 A record (``None`` when DNS is not configured).
        tls_subscription: The Fastly ``TlsSubscription`` (``None`` when TLS
            is not managed).
    """

    service: fastly.ServiceVcl
    dns_record: route53.Record | None
    tls_subscription: fastly.TlsSubscription | None

    def __init__(
        self,
        name: str,
        config: OLFastlyServiceVCLConfig,
        opts: ResourceOptions | None = None,
    ) -> None:
        """Create an OLFastlyServiceVCL component resource.

        Args:
            name: The Pulumi resource name for this component.
            config: Configuration describing the desired Fastly service.
            opts: Standard Pulumi resource options.  Must include a
                ``fastly.Provider`` when TLS is managed.
        """
        # Capture the Fastly provider before super().__init__ processes opts.
        # The raw provider reference is needed for invoke calls
        # (e.g. fastly.get_tls_configuration).
        self._fastly_provider: pulumi.ProviderResource | None = (
            opts.provider if opts else None
        )

        super().__init__(
            "ol:services:Fastly:ServiceVCL",
            name,
            None,
            opts,
        )

        child_opts = ResourceOptions(parent=self)

        service_kwargs = self._build_service_kwargs(config, child_opts)
        self.service = fastly.ServiceVcl(
            f"{name}-fastly-service",
            **service_kwargs,
        )

        self.tls_subscription = None
        self.dns_record = None
        if config.tls.managed:
            self._create_managed_tls(name, config)
        elif config.dns:
            self._create_simple_dns(name, config)

        self.register_outputs({"service_id": self.service.id})

    def _build_service_kwargs(
        self,
        config: OLFastlyServiceVCLConfig,
        child_opts: ResourceOptions,
    ) -> dict[str, Any]:
        """Assemble all keyword arguments for the ``fastly.ServiceVcl`` call."""
        service_kwargs: dict[str, Any] = {
            "name": config.service_name,
            "comment": config.comment,
            "backends": config.backends,
            "domains": config.domains,
            "gzips": _build_gzips(config),
            "headers": _build_headers(config),
            "request_settings": _build_request_settings(config),
            "snippets": config.snippets,
            "conditions": config.conditions,
            "cache_settings": config.cache_settings,
            "dictionaries": config.dictionaries,
        }

        product_enablement = _build_product_enablement(config)
        if product_enablement:
            service_kwargs["product_enablement"] = product_enablement
        if config.image_optimizer_default_settings:
            service_kwargs["image_optimizer_default_settings"] = (
                config.image_optimizer_default_settings
            )

        logging_https = _build_https_logging(config)
        if logging_https:
            service_kwargs["logging_https"] = logging_https

        logging_s3s = _build_s3_logging(config)
        if logging_s3s:
            service_kwargs["logging_s3s"] = logging_s3s

        if config.response_objects:
            service_kwargs["response_objects"] = config.response_objects
        if config.stale_if_error:
            service_kwargs["stale_if_error"] = True
        if config.default_ttl is not None:
            service_kwargs["default_ttl"] = config.default_ttl

        # Merge caller aliases with the automatic top-level alias (if any).
        # All aliases on the child ServiceVcl must reference the original
        # top-level resource (parent=None / ROOT_STACK_RESOURCE), because
        # before the component existed every ServiceVcl was a root resource.
        # Aliases created via Alias(name=...) without an explicit parent get
        # parent=Ellipsis (inherit-from-resource), which would incorrectly
        # resolve to the component child namespace instead of the root stack.
        all_aliases = [_normalize_alias(a) for a in config.aliases]
        if config.service_vcl_resource_name:
            all_aliases.append(
                Alias(name=config.service_vcl_resource_name, parent=ROOT_STACK_RESOURCE)
            )
        service_opts = child_opts
        if all_aliases:
            service_opts = ResourceOptions.merge(
                child_opts,
                ResourceOptions(aliases=all_aliases),
            )
        service_kwargs["opts"] = service_opts

        return service_kwargs

    def _create_managed_tls(
        self,
        name: str,
        config: OLFastlyServiceVCLConfig,
    ) -> None:
        """Create a managed TLS subscription with DNS validation and an A record."""
        tls_configuration = fastly.get_tls_configuration(
            default=False,
            name="TLS v1.3",
            tls_protocols=["1.2", "1.3"],
            opts=InvokeOptions(provider=self._fastly_provider),
        )

        child_opts = ResourceOptions(parent=self)
        self.tls_subscription = fastly.TlsSubscription(
            f"{name}-tls-subscription",
            certificate_authority=config.tls.certificate_authority,
            domains=self.service.domains.apply(
                lambda domains: [domain.name for domain in domains or []]
            ),
            configuration_id=tls_configuration.id,
            opts=child_opts,
        )

        self.tls_subscription.managed_dns_challenges.apply(
            lambda challenges: (
                fastly_certificate_validation_records(challenges) if challenges else []
            )
        )

        fastly.TlsSubscriptionValidation(
            f"{name}-tls-subscription-validation",
            subscription_id=self.tls_subscription.id,
            opts=child_opts,
        )

        if config.dns:
            zone_id = config.dns.zone_id or lookup_zone_id_from_domain(
                config.dns.frontend_domain
            )
            self.dns_record = route53.Record(
                f"{name}-fastly-dns-record",
                name=config.dns.frontend_domain,
                type="A",
                ttl=FIVE_MINUTES,
                records=[
                    record["record_value"]
                    for record in (tls_configuration.dns_records or [])
                    if record["record_type"] == "A"
                ],
                zone_id=zone_id,
                allow_overwrite=True,
                opts=ResourceOptions(parent=self),
            )

    def _create_simple_dns(
        self,
        name: str,
        config: OLFastlyServiceVCLConfig,
    ) -> None:
        """Create a simple DNS A record using the static FASTLY_A_TLS_1_3 addresses."""
        if not config.dns:
            return
        zone_id = config.dns.zone_id or lookup_zone_id_from_domain(
            config.dns.frontend_domain
        )
        self.dns_record = route53.Record(
            f"{name}-fastly-dns-record",
            name=config.dns.frontend_domain,
            type="A",
            ttl=FIVE_MINUTES,
            records=[str(addr) for addr in FASTLY_A_TLS_1_3],
            zone_id=zone_id,
            allow_overwrite=True,
            opts=ResourceOptions.merge(
                ResourceOptions(parent=self),
                ResourceOptions(delete_before_replace=True),
            ),
        )
