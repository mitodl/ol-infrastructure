# ruff: noqa: ERA001, C416, E501
"""
This is a service components that replaces a number of "boilerplate" kubernetes
calls we currently make into one convenient callable package.
"""

from pathlib import Path
from typing import Any, Literal, Optional

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, Output, ResourceOptions
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    field_validator,
    model_validator,
)

from bridge.lib.magic_numbers import (
    DEFAULT_NGINX_PORT,
    DEFAULT_UWSGI_PORT,
    MAXIMUM_K8S_NAME_LENGTH,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


def truncate_k8s_metanames(name: str) -> str:
    """
    Sanitize the names we use for k8s objects
    """
    return name[:MAXIMUM_K8S_NAME_LENGTH].rstrip("-_.")


class OLApplicationK8sConfiguration(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_root: Path
    application_config: dict[str, str]
    application_name: str
    application_namespace: str
    application_lb_service_name: str
    application_lb_service_port_name: str
    k8s_global_labels: dict[str, str]
    db_creds_secret_name: str
    redis_creds_secret_name: str
    static_secrets_name: str
    application_security_group_id: Output[str]
    application_security_group_name: Output[str]
    application_image_repository: str
    application_docker_tag: str
    vault_k8s_resource_auth_name: str
    import_nginx_config: bool
    resource_requests: dict[str, str] = Field(
        default={"cpu": "250m", "memory": "300Mi"}
    )
    resource_limits: dict[str, str] = Field(
        default={"cpu": "500m", "memory": "600Mi"},
    )
    init_migrations: bool = Field(default=True)
    init_collectstatic: bool = Field(default=True)

    # See https://www.pulumi.com/docs/reference/pkg/python/pulumi/#pulumi.Output.from_input
    # for docs. This unwraps the value so Pydantic can store it in the config class.
    @field_validator("application_security_group_id")
    @classmethod
    def validate_sec_group_id(cls, application_security_group_id: Output[str]):
        return Output.from_input(application_security_group_id)

    @field_validator("application_security_group_name")
    @classmethod
    def validate_sec_group_name(cls, application_security_group_name: Output[str]):
        return Output.from_input(application_security_group_name)


stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"


class OLApplicationK8s(ComponentResource):
    """
    Main K8s component resource class
    """

    def __init__(
        self,
        ol_app_k8s_config: OLApplicationK8sConfiguration,
        opts: Optional[ResourceOptions] = None,
    ):
        """
        It's .. the constructor. Shaddap Ruff :)
        """
        super().__init__(
            "ol:infrastructure:components:services:OLApplicationK8s",
            ol_app_k8s_config.application_namespace,
            None,
            opts=opts,
        )
        resource_options = ResourceOptions(parent=self)
        self.application_lb_service_name: str = (
            ol_app_k8s_config.application_lb_service_name
        )
        self.application_lb_service_port_name: str = (
            ol_app_k8s_config.application_lb_service_port_name
        )

        if ol_app_k8s_config.import_nginx_config:
            application_nginx_configmap = kubernetes.core.v1.ConfigMap(
                f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-nginx-configmap",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name="nginx-config",
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=ol_app_k8s_config.k8s_global_labels,
                ),
                data={
                    "web.conf": ol_app_k8s_config.project_root.joinpath(
                        "files/web.conf"
                    ).read_text(),
                },
                opts=resource_options,
            )

        # Build a list of not-sensitive env vars for the deployment config
        application_deployment_env_vars = []
        for k, v in (ol_app_k8s_config.application_config).items():
            application_deployment_env_vars.append(
                kubernetes.core.v1.EnvVarArgs(
                    name=k,
                    value=v,
                )
            )
        application_deployment_env_vars.append(
            kubernetes.core.v1.EnvVarArgs(name="PORT", value=str(DEFAULT_UWSGI_PORT))
        )

        # Build a list of sensitive env vars for the deployment config via envFrom
        application_deployment_envfrom = [
            # Database creds
            kubernetes.core.v1.EnvFromSourceArgs(
                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                    name=ol_app_k8s_config.db_creds_secret_name,
                ),
            ),
            # Redis Configuration
            kubernetes.core.v1.EnvFromSourceArgs(
                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                    name=ol_app_k8s_config.redis_creds_secret_name,
                ),
            ),
            # static secrets from secrets-application/secrets
            kubernetes.core.v1.EnvFromSourceArgs(
                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                    name=ol_app_k8s_config.static_secrets_name,
                ),
            ),
        ]

        app_image = f"{ol_app_k8s_config.application_image_repository}:{ol_app_k8s_config.application_docker_tag}"
        init_containers = []
        if ol_app_k8s_config.init_collectstatic:
            init_containers.append(
                # Run database migrations at startup
                kubernetes.core.v1.ContainerArgs(
                    name="migrate",
                    image=app_image,
                    command=["python3", "manage.py", "migrate", "--noinput"],
                    image_pull_policy="IfNotPresent",
                    env=application_deployment_env_vars,
                    env_from=application_deployment_envfrom,
                )
            )

        if ol_app_k8s_config.init_migrations:
            init_containers.append(
                kubernetes.core.v1.ContainerArgs(
                    name="collectstatic",
                    image=app_image,
                    command=["python3", "manage.py", "collectstatic", "--noinput"],
                    image_pull_policy="IfNotPresent",
                    env=application_deployment_env_vars,
                    env_from=application_deployment_envfrom,
                    volume_mounts=[
                        kubernetes.core.v1.VolumeMountArgs(
                            name="staticfiles",
                            mount_path="/src/staticfiles",
                        ),
                    ],
                )
            )

        # Create a deployment resource to manage the application pods
        application_labels = ol_app_k8s_config.k8s_global_labels | {
            "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}-application",
            "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                truncate_k8s_metanames
            ),
        }

        _application_deployment = kubernetes.apps.v1.Deployment(
            f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-deployment",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=truncate_k8s_metanames(
                    f"{ol_app_k8s_config.application_name}-app"
                ),
                namespace=ol_app_k8s_config.application_namespace,
                labels=application_labels,
            ),
            spec=kubernetes.apps.v1.DeploymentSpecArgs(
                # TODO @Ardiea: Add horizontial pod autoscaler  # noqa: TD003, FIX002
                replicas=1,
                selector=kubernetes.meta.v1.LabelSelectorArgs(
                    match_labels=application_labels,
                ),
                # Limits the chances of simulatious pod restarts -> db migrations
                # (hopefully)
                strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
                    type="RollingUpdate",
                    rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                        max_surge=0,
                        max_unavailable=1,
                    ),
                ),
                template=kubernetes.core.v1.PodTemplateSpecArgs(
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        labels=application_labels,
                    ),
                    spec=kubernetes.core.v1.PodSpecArgs(
                        volumes=[
                            kubernetes.core.v1.VolumeArgs(
                                name="staticfiles",
                                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                            ),
                            kubernetes.core.v1.VolumeArgs(
                                name="nginx-config",
                                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                    name=application_nginx_configmap.metadata.name,
                                    items=[
                                        kubernetes.core.v1.KeyToPathArgs(
                                            key="web.conf",
                                            path="web.conf",
                                        ),
                                    ],
                                ),
                            ),
                        ],
                        init_containers=init_containers,
                        dns_policy="ClusterFirst",
                        containers=[
                            # nginx container infront of uwsgi
                            kubernetes.core.v1.ContainerArgs(
                                name="nginx",
                                image="nginx:1.9.5",
                                ports=[
                                    kubernetes.core.v1.ContainerPortArgs(
                                        container_port=DEFAULT_NGINX_PORT
                                    )
                                ],
                                image_pull_policy="IfNotPresent",
                                resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                    requests=ol_app_k8s_config.resource_requests,
                                    limits=ol_app_k8s_config.resource_limits,
                                ),
                                volume_mounts=[
                                    kubernetes.core.v1.VolumeMountArgs(
                                        name="staticfiles",
                                        mount_path="/src/staticfiles",
                                    ),
                                    kubernetes.core.v1.VolumeMountArgs(
                                        name="nginx-config",
                                        mount_path="/etc/nginx/conf.d/web.conf",
                                        sub_path="web.conf",
                                        read_only=True,
                                    ),
                                ],
                            ),
                            # Actual application run with uwsgi
                            kubernetes.core.v1.ContainerArgs(
                                name=f"{ol_app_k8s_config.application_name}-app",
                                image=app_image,
                                ports=[
                                    kubernetes.core.v1.ContainerPortArgs(
                                        container_port=DEFAULT_UWSGI_PORT
                                    )
                                ],
                                image_pull_policy="IfNotPresent",
                                resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                    requests={"cpu": "250m", "memory": "300Mi"},
                                    limits={"cpu": "500m", "memory": "600Mi"},
                                ),
                                env=application_deployment_env_vars,
                                env_from=application_deployment_envfrom,
                                volume_mounts=[
                                    kubernetes.core.v1.VolumeMountArgs(
                                        name="staticfiles",
                                        mount_path="/src/staticfiles",
                                    ),
                                ],
                            ),
                        ],
                    ),
                ),
            ),
            opts=resource_options,
        )

        # A kubernetes service resource to act as load balancer for the app instances
        _application_service = kubernetes.core.v1.Service(
            f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-service",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=truncate_k8s_metanames(
                    ol_app_k8s_config.application_lb_service_name
                ),
                namespace=ol_app_k8s_config.application_namespace,
                labels=ol_app_k8s_config.k8s_global_labels,
            ),
            spec=kubernetes.core.v1.ServiceSpecArgs(
                selector=application_labels,
                ports=[
                    kubernetes.core.v1.ServicePortArgs(
                        name=ol_app_k8s_config.application_lb_service_port_name,
                        port=DEFAULT_NGINX_PORT,
                        target_port=DEFAULT_NGINX_PORT,
                        protocol="TCP",
                    ),
                ],
                type="ClusterIP",
            ),
            opts=resource_options,
        )

        _application_pod_security_group_policy = (
            kubernetes.apiextensions.CustomResource(
                f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-application-pod-security-group-policy",
                api_version="vpcresources.k8s.aws/v1beta1",
                kind="SecurityGroupPolicy",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=ol_app_k8s_config.application_security_group_name.apply(
                        truncate_k8s_metanames
                    ),
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=ol_app_k8s_config.k8s_global_labels,
                ),
                spec={
                    "podSelector": {
                        "matchLabels": {
                            "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                                truncate_k8s_metanames
                            ),
                        },
                    },
                    "securityGroups": {
                        "groupIds": [
                            ol_app_k8s_config.application_security_group_id,
                        ],
                    },
                },
            ),
        )


class OLApisixPluginConfig(BaseModel):
    name: str
    enable: bool = True
    secret_ref: Optional[str] = Field(
        None,
        alias="secretRef",
    )
    config: dict[str, Any] = {}


class OLApisixRouteConfig(BaseModel):
    route_name: str
    priority: int = 0
    shared_plugin_config_name: Optional[str] = None
    plugins: list[OLApisixPluginConfig] = []
    hosts: list[str] = []
    paths: list[str] = []
    backend_service_name: Optional[str] = None
    backend_service_port: Optional[str] = None
    # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/#service-resolution-granularity
    backend_resolve_granularity: Literal["endpoint", "service"] = "service"
    upstream: Optional[str] = None
    websocket: bool = False

    @model_validator(mode="after")
    def check_backend_or_upstream(self) -> "OLApisixRouteConfig":
        upstream: Optional[str] = self.upstream
        backend_service_name: Optional[str] = self.backend_service_name
        backend_service_port: Optional[str] = self.backend_service_port

        if upstream is not None:
            if backend_service_name is not None or backend_service_port is not None:
                msg = "If 'upstream' is provided, 'backend_service_name' and 'backend_service_port' must not be provided."
                raise ValueError(msg)
        elif backend_service_name is None or backend_service_port is None:
            msg = "If 'upstream' is not provided, both 'backend_service_name' and 'backend_service_port' must be provided."
            raise ValueError(msg)
        return self


class OLApisixRoute(pulumi.ComponentResource):
    """
    Route configuration for apisix
    Defines and creates an "ApisixRoute" resource in the k8s cluster
    """

    def __init__(
        self,
        name: str,
        route_configs: list[OLApisixRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixRoute", name, None, opts
        )

        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        self.apisix_route_resource = kubernetes.apiextensions.CustomResource(
            f"OLApisixRoute-{name}",
            api_version="apisix.apache.org/v2",
            kind="ApisixRoute",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=name,
                labels=k8s_labels,
                namespace=k8s_namespace,
            ),
            spec={
                "http": self.__build_route_list(route_configs),
            },
            opts=resource_options.merge(
                pulumi.ResourceOptions(delete_before_replace=True)
            ),
        )

    @classmethod
    def __build_route_list(
        cls, route_configs: list[OLApisixRouteConfig]
    ) -> list[dict[str, Any]]:
        routes = []
        for route_config in route_configs:
            route = {
                "name": route_config.route_name,
                "priority": route_config.priority,
                "plugin_config_name": route_config.shared_plugin_config_name,
                "plugins": [p.model_dump(by_alias=True) for p in route_config.plugins],
                "match": {
                    "hosts": route_config.hosts,
                    "paths": route_config.paths,
                },
                "websocket": route_config.websocket,
            }
            if route_config.upstream:
                route["upstreams"] = [{"name": route_config.upstream}]
            else:
                route["backends"] = [
                    {
                        "serviceName": route_config.backend_service_name,
                        "servicePort": route_config.backend_service_port,
                        "resolveGranularity": route_config.backend_resolve_granularity,
                    }
                ]
            routes.append(route)
        return routes


class OLApisixOIDCConfig(BaseModel):
    application_name: str
    k8s_labels: dict[str, str] = {}
    k8s_namespace: str
    vault_mount: str = "secret-operations"
    vault_mount_type: str = "kv-v1"
    vault_path: str
    vaultauth: str

    oidc_bearer_only: bool = False
    oidc_introspection_endpoint_auth_method: str = "client_secret_basic"
    oidc_logout_path: str = "/logout/oidc"
    oidc_post_logout_redirect_uri: str = "/"
    oidc_refresh_session_interval: NonNegativeInt = 1800
    oidc_renew_access_token_on_expiry: bool = True
    oidc_scope: str = "openid profile email"
    oidc_session_contents: dict[str, bool] = {
        "access_token": True,
        "enc_id_token": True,
        "id_token": True,
        "user": True,
    }
    oidc_session_cookie_lifetime: NonNegativeInt = 0
    oidc_ssl_verify: bool = False
    oidc_use_session_secret: bool = False


class OLApisixOIDCResources(pulumi.ComponentResource):
    """
    OIDC configuration for apisix
    Defines and creates an "OLVaultK8SSecret" resource in the k8s cluster
    Also provides helper functions for creating config blocks for the oidc plugin
    """

    def __init__(
        self,
        name: str,
        oidc_config: OLApisixOIDCConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixOIDCResources", name, None, opts
        )

        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        self.secret_name = f"ol-apisix-{oidc_config.application_name}-oidc-secrets"

        __templates = {
            "client_id": '{{ get .Secrets "client_id" }}',
            "client_secret": '{{ get .Secrets "client_secret" }}',
            "realm": '{{ get .Secrets "realm_name" }}',
            "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
        }

        if oidc_config.oidc_use_session_secret:
            __templates["session_secret"] = '{{ get .Secrets "secret" }}'  # noqa: S105

        self.oidc_secrets = OLVaultK8SSecret(
            f"{oidc_config.application_name}-oidc-secrets",
            resource_config=OLVaultK8SStaticSecretConfig(
                dest_secret_labels=oidc_config.k8s_labels,
                dest_secret_name=self.secret_name,
                exclude_raw=True,
                excludes=[".*"],
                labels=oidc_config.k8s_labels,
                mount=oidc_config.vault_mount,
                mount_type=oidc_config.vault_mount_type,
                name=self.secret_name,
                namespace=oidc_config.k8s_namespace,
                path=oidc_config.vault_path,
                refresh_after="1m",
                templates=__templates,
                vaultauth=oidc_config.vaultauth,
            ),
            opts=resource_options.merge(
                pulumi.ResourceOptions(delete_before_replace=True)
            ),
        )

        self.base_oidc_config = {
            "scope": oidc_config.oidc_scope,
            "bearer_only": oidc_config.oidc_bearer_only,
            "introspection_endpoint_auth_method": oidc_config.oidc_introspection_endpoint_auth_method,
            "ssl_verify": oidc_config.oidc_ssl_verify,
            "renew_access_token_on_expiry": oidc_config.oidc_renew_access_token_on_expiry,
            "refresH_session_interval": oidc_config.oidc_refresh_session_interval,
            "logout_path": oidc_config.oidc_logout_path,
            "post_logout_redirect_uri": oidc_config.oidc_post_logout_redirect_uri,
        }

        if oidc_config.oidc_session_cookie_lifetime:
            self.base_oidc_config["session_cookie_lifetime"] = {
                "cookie": {
                    "lifetime": 60 * oidc_config.oidc_session_cookie_lifetime,
                },
            }

        if oidc_config.oidc_session_contents:
            self.base_oidc_config["session_contents"] = (
                oidc_config.oidc_session_contents
            )

    def get_base_oidc_config(self, unauth_action: str) -> dict[str, Any]:
        return {
            **self.base_oidc_config,
            "unauth_action": unauth_action,
        }

    def get_full_oidc_plugin_config(self, unauth_action: str) -> dict[str, Any]:
        return {
            "name": "openid-connect",
            "enable": True,
            "secretRef": self.secret_name,
            "config": {
                **self.get_base_oidc_config(unauth_action),
            },
        }


# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_pluginconfig_v2/
class OLApisixSharedPluginsConfig(BaseModel):
    application_name: str
    resource_suffix: str = "shared-plugins"
    enable_defaults: bool = True
    k8s_labels: dict[str, str] = {}
    k8s_namespace: str
    plugins: list[dict[str, Any]] = []


class OLApisixSharedPlugins(pulumi.ComponentResource):
    """
    Shared plugin configuration for apisix
    Defines and creates an "ApisixPluginConfig" resource in the k8s cluster
    """

    def __init__(
        self,
        name: str,
        plugin_config: OLApisixSharedPluginsConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixSharedPlugin", name, None, opts
        )

        __default_plugins: list[dict[str, Any]] = [
            {
                "name": "redirect",
                "enable": True,
                "config": {
                    "http_to_https": True,
                },
            },
            {
                "name": "cors",
                "enable": True,
                "config": {
                    "allow_origins": "**",
                    "allow_methods": "**",
                    "allow_headers": "**",
                    "allow_credentials": True,
                },
            },
            {
                "name": "response-rewrite",
                "enable": True,
                "config": {
                    "headers": {
                        "set": {
                            "Referrer-Policy": "origin",
                        }
                    },
                },
            },
        ]

        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        if plugin_config.enable_defaults:
            plugin_config.plugins.extend(__default_plugins)

        self.resource_name = (
            f"{plugin_config.application_name}-{plugin_config.resource_suffix}"
        )
        self.shared_plugin_apisix_pluginconfig_resource = (
            kubernetes.apiextensions.CustomResource(
                f"OLApisixSharedPlugin-{self.resource_name}",
                api_version="apisix.apache.org/v2",
                kind="ApisixPluginConfig",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=self.resource_name,
                    labels=plugin_config.k8s_labels,
                    namespace=plugin_config.k8s_namespace,
                ),
                spec={
                    "plugins": plugin_config.plugins,
                },
                opts=resource_options,
            )
        )


class OLApisixExternalUpstreamConfig(BaseModel):
    application_name: str
    resource_suffix: str = "external-upstream"
    k8s_labels: dict[str, str] = {}
    k8s_namespace: str
    external_hostname: str
    scheme: str = "https"


class OLApisixExternalUpstream(pulumi.ComponentResource):
    """
    External upstream configuration for apisix
    Defines and creates an "ApisixUpstream" resource in the k8s cluster
    This is for a service that is hosted outside of kubernetes but we want
    to have APISIX in front of it anyways.
    """

    def __init__(
        self,
        name: str,
        external_upstream_config: OLApisixExternalUpstreamConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixExternalUpstream", name, None, opts
        )
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        self.resource_name = f"{external_upstream_config.application_name}-{external_upstream_config.resource_suffix}"
        self.shared_plugin_apisix_pluginconfig_resource = (
            kubernetes.apiextensions.CustomResource(
                f"OLApisixExternalService-{self.resource_name}",
                api_version="apisix.apache.org/v2",
                kind="ApisixUpstream",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=self.resource_name,
                    labels=external_upstream_config.k8s_labels,
                    namespace=external_upstream_config.k8s_namespace,
                ),
                spec={
                    "scheme": external_upstream_config.scheme,
                    "externalNodes": [
                        {
                            "type": "Domain",
                            "name": external_upstream_config.external_hostname,
                        },
                    ],
                },
                opts=resource_options,
            )
        )
