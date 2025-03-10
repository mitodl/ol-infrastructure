"""
This is a service components that replaces a number of "boilerplate" kubernetes calls we currently make into one convenient callable package.
"""
from pathlib import Path
from typing import Optional

import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, Config, ResourceOptions
from pydantic import BaseModel

from bridge.lib.magic_numbers import DEFAULT_NGINX_PORT, DEFAULT_UWSGI_PORT
from bridge.settings.github.team_members import DEVOPS_MIT
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


class OLAppK8sConfiguration(BaseModel):
    application_config: Config
    image_repo: str
    application_namespace: str
    k8s_global_labels: dict[str,str]
    db_creds_secret_name: str
    redis_creds_secret_name: str
    static_secrets_name: str
    application_DOCKER_TAG: str

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

class OLAppK8s(ComponentResource):
    """
    Main K8s component resource class
    """

    def __init__(
        self,
        ol_app_k8s_config: OLAppK8sConfiguration,
        opts: Optional[ResourceOptions] = None,
    ):
        self.ol_app_k8s_config = ol_app_k8s_config
        super().__init__(
            "ol:infrastructure:aws:OLAppK8s",
            self.ol_app_k8s_config.application_namespace,
            None,
            opts,
        )
        application_nginx_configmap = kubernetes.core.v1.ConfigMap(
            f"unified-application-{stack_info.env_suffix}-nginx-configmap",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="nginx-config",
                namespace=ol_app_k8s_config.application_namespace,
                labels=ol_app_k8s_config.k8s_global_labels,
            ),
            data={
                "web.conf": Path(__file__).parent.joinpath("files/web.conf").read_text(),
            },
            opts=ResourceOptions(
                delete_before_replace=True,
            ),
        )

        # Build a list of not-sensitive env vars for the deployment config
        application_deployment_env_vars = []
        for k, v in (self.ol_app_k8s_config.application_config.require_object("env_vars") or {}).items():
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
                    name=self.ol_app_k8s_config.db_creds_secret_name,
                ),
            ),
            # Redis Configuration
            kubernetes.core.v1.EnvFromSourceArgs(
                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                    name=self.ol_app_k8s_config.redis_creds_secret_name,
                ),
            ),
            # static secrets from secrets-application/secrets
            kubernetes.core.v1.EnvFromSourceArgs(
                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                    name=self.ol_app_k8s_config.static_secrets_name,
                ),
            ),
        ]

        init_containers = [
            # Run database migrations at startup
            kubernetes.core.v1.ContainerArgs(
                name="migrate",
                image=f"mitodl/unified-application-app-main:{self.ol_app_k8s_config.application_DOCKER_TAG}",
                command=["python3", "manage.py", "migrate", "--noinput"],
                image_pull_policy="IfNotPresent",
                env=application_deployment_env_vars,
                env_from=application_deployment_envfrom,
            ),
            kubernetes.core.v1.ContainerArgs(
                name="collectstatic",
                image=f"mitodl/unified-application-app-main:{self.ol_app_k8s_config.application_DOCKER_TAG}",
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
            ),
        ] + [
            kubernetes.core.v1.ContainerArgs(
                name=f"promote-{mit_username}-to-superuser",
                image=f"mitodl/unified-application-app-main:{self.ol_app_k8s_config.application_DOCKER_TAG}",
                # Jank that forces the promotion to always exit successfully
                command=["/bin/bash"],
                args=[
                    "-c",
                    f"./manage.py promote_user --promote --superuser '{mit_username}@mit.edu'; exit 0",  # noqa: E501
                ],
                image_pull_policy="IfNotPresent",
                env=application_deployment_env_vars,
                env_from=application_deployment_envfrom,
            )
            for mit_username in DEVOPS_MIT
        ]

        # Create a deployment resource to manage the application pods
        application_labels = self.ol_app_k8s_config.k8s_global_labels | {
            "ol.mit.edu/application": "unified-application",
            "ol.mit.edu/pod-security-group": "application-app",
        }

        application_deployment_resource = kubernetes.apps.v1.Deployment(
            f"unified-application-{stack_info.env_suffix}-deployment",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="application-app",
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=application_labels,
            ),
            spec=kubernetes.apps.v1.DeploymentSpecArgs(
                # TODO @Ardiea: Add horizontial pod autoscaler  # noqa: TD003, FIX002
                replicas=1,
                selector=kubernetes.meta.v1.LabelSelectorArgs(
                    match_labels=application_labels,
                ),
                # Limits the chances of simulatious pod restarts -> db migrations (hopefully)
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
                                    requests={"cpu": "50m", "memory": "64Mi"},
                                    limits={"cpu": "100m", "memory": "128Mi"},
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
                                name="application-app",
                                image=f"mitodl/unified-application-app-main:{self.ol_app_k8s_config.application_DOCKER_TAG}",
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
            opts=ResourceOptions(
                delete_before_replace=True,
                depends_on=[db_creds_secret, redis_creds],
            ),
        )

        # A kubernetes service resource to act as load balancer for the app instances
        application_service_name = "application-app"
        application_service_port_name = "http"
        application_service = kubernetes.core.v1.Service(
            f"unified-application-{stack_info.env_suffix}-service",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=application_service_name,
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=self.ol_app_k8s_config.k8s_global_labels,
            ),
            spec=kubernetes.core.v1.ServiceSpecArgs(
                selector=application_labels,
                ports=[
                    kubernetes.core.v1.ServicePortArgs(
                        name=application_service_port_name,
                        port=DEFAULT_NGINX_PORT,
                        target_port=DEFAULT_NGINX_PORT,
                        protocol="TCP",
                    ),
                ],
                type="ClusterIP",
            ),
            opts=ResourceOptions(delete_before_replace=True),
        )

        application_pod_security_group_policy = (
            kubernetes.apiextensions.CustomResource(
                f"unified-application-{stack_info.env_suffix}-application-pod-security-group-policy",
                api_version="vpcresources.k8s.aws/v1beta1",
                kind="SecurityGroupPolicy",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name="application-app",
                    namespace=self.ol_app_k8s_config.application_namespace,
                    labels=self.ol_app_k8s_config.k8s_global_labels,
                ),
                spec={
                    "podSelector": {
                        "matchLabels": {"ol.mit.edu/pod-security-group": "application-app"},
                    },
                    "securityGroups": {
                        "groupIds": [
                            application_application_security_group.id,
                        ],
                    },
                },
            ),
        )

        # Create the apisix custom resources since it doesn't support gateway-api yet

        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_plugin_config/
        # Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_pluginconfig_v2/
        shared_plugin_config_name = "shared-plugin-config"
        application_https_apisix_pluginconfig = kubernetes.apiextensions.CustomResource(
            f"unified-application-{stack_info.env_suffix}-https-apisix-pluginconfig",
            api_version="apisix.apache.org/v2",
            kind="ApisixPluginConfig",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=shared_plugin_config_name,
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=self.ol_app_k8s_config.k8s_global_labels,
            ),
            spec={
                "plugins": [
                    {
                        "name": "cors",
                        "enable": True,
                        "config": {
                            "allow_origins": "**",
                            "allow_methods": "**",
                            "allow_headers": "**",
                            "allow_credential": True,
                        },
                    },
                    {
                        "name": "response-rewrite",
                        "enable": True,
                        "config": {
                            "headers": {
                                "set": {
                                    "Referrer-Policy": "origin",
                                },
                            },
                        },
                    },
                ],
            },
        )

        # Load open-id-connect secrets into a k8s secret via VSO
        oidc_secret_name = "oidc-secrets"  # pragma: allowlist secret  # noqa: S105
        oidc_secret = OLVaultK8SSecret(
            name=f"unified-application-{stack_info.env_suffix}-oidc-secrets",
            resource_config=OLVaultK8SStaticSecretConfig(
                name="oidc-static-secrets",
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=self.ol_app_k8s_config.k8s_global_labels,
                dest_secret_name=oidc_secret_name,
                dest_secret_labels=self.ol_app_k8s_config.k8s_global_labels,
                mount="secret-operations",
                mount_type="kv-v1",
                path="sso/unified-application",
                excludes=[".*"],
                exclude_raw=True,
                templates={
                    "client_id": '{{ get .Secrets "client_id" }}',
                    "client_secret": '{{ get .Secrets "client_secret" }}',
                    "realm": '{{ get .Secrets "realm_name" }}',
                    "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True,
                parent=vault_k8s_resources,
                depends_on=[application_static_vault_secrets],
            ),
        )

        # ApisixUpstream resources don't seem to work but we don't really need them?
        # Ref: https://github.com/apache/apisix-ingress-controller/issues/1655
        # Ref: https://github.com/apache/apisix-ingress-controller/issues/1855

        # Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_route_v2/
        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/
        application_https_apisix_route = kubernetes.apiextensions.CustomResource(
            f"unified-application-{stack_info.env_suffix}-https-apisix-route",
            api_version="apisix.apache.org/v2",
            kind="ApisixRoute",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="application-https",
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=self.ol_app_k8s_config.k8s_global_labels,
            ),
            spec={
                "http": [
                    {
                        # unauthenticated routes, including assests and checkout callback API
                        "name": "ue-unauth",
                        "priority": 1,
                        "match": {
                            "hosts": [
                                self.ol_app_k8s_config.application_config.require("backend_domain"),
                            ],
                            "paths": [
                                "/api/*",
                                "/_/*",
                                "/logged_out/*",
                                "/auth/*",
                                "/static/*",
                                "/favicon.ico",
                                "/checkout/*",
                            ],
                        },
                        "plugin_config_name": shared_plugin_config_name,
                        "backends": [
                            {
                                "serviceName": application_service_name,
                                "servicePort": application_service_port_name,
                            }
                        ],
                    },
                    {
                        # wildcard route for the rest of the system - auth required
                        "name": "ue-default",
                        "priority": 0,
                        "plugins": [
                            # Ref: https://apisix.apache.org/docs/apisix/plugins/openid-connect/
                            {
                                "name": "openid-connect",
                                "enable": True,
                                # Get all the sensitive parts of this config from a secret
                                "secretRef": oidc_secret_name,
                                "config": {
                                    "scope": "openid profile ol-profile",
                                    "bearer_only": False,
                                    "introspection_endpoint_auth_method": "client_secret_post",
                                    "ssl_verify": False,
                                    "logout_path": "/logout",
                                    "discovery": "https://sso-qa.ol.mit.edu/realms/olapps/.well-known/openid-configuration",
                                    # Lets let the app handle this because we have an etcd
                                    # control-plane
                                    # "session": {
                                    #    "secret": "at_least_16_characters",  # pragma: allowlist secret  # noqa: E501
                                    # },
                                },
                            },
                        ],
                        "plugin_config_name": shared_plugin_config_name,
                        "match": {
                            "hosts": [
                                self.ol_app_k8s_config.application_config.require("backend_domain"),
                            ],
                            "paths": [
                                "/cart/*",
                                "/admin/*",
                                "/establish_session/*",
                                "/logout",
                            ],
                        },
                        "backends": [
                            {
                                "serviceName": application_service_name,
                                "servicePort": application_service_port_name,
                            }
                        ],
                    },
                    # Strip trailing slack from logout redirect
                    {
                        "name": "ue-logout-redirect",
                        "priority": 0,
                        "plugins": [
                            {
                                "name": "redirect",
                                "enable": True,
                                "config": {
                                    "uri": "/logout",
                                },
                            },
                        ],
                        "match": {
                            "hosts": [
                                self.ol_app_k8s_config.application_config.require("backend_domain"),
                            ],
                            "paths": [
                                "/logout/*",
                            ],
                        },
                        "backends": [
                            {
                                "serviceName": application_service_name,
                                "servicePort": application_service_port_name,
                            }
                        ],
                    },
                ]
            },
            opts=ResourceOptions(
                delete_before_replace=True,
                depends_on=[application_service],
            ),
        )

        # Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_tls_v2/
        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
        application_https_apisix_tls = kubernetes.apiextensions.CustomResource(
            f"unified-application-{stack_info.env_suffix}-https-apisix-tls",
            api_version="apisix.apache.org/v2",
            kind="ApisixTls",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="application-https",
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=self.ol_app_k8s_config.k8s_global_labels,
            ),
            spec={
                "hosts": [self.ol_app_k8s_config.application_config.require("backend_domain")],
                # Use the shared ol-wildcard cert loaded into every cluster
                "secret": {
                    "name": "ol-wildcard-cert",
                    "namespace": "operations",
                },
            },
        )


