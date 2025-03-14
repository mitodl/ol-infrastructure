# ruff: noqa: ERA001, C416
"""
This is a service components that replaces a number of "boilerplate" kubernetes
calls we currently make into one convenient callable package.
"""

from pathlib import Path
from typing import Optional

import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, Output, ResourceOptions, debug
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bridge.lib.magic_numbers import (
    DEFAULT_NGINX_PORT,
    DEFAULT_UWSGI_PORT,
    MAXIMUM_K8S_NAME_LENGTH,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


def truncate_k8s_metanames(name: str) -> str:
    """
    Sanitize the names we use for k8s objects
    """
    truncated_name = name[:MAXIMUM_K8S_NAME_LENGTH].rstrip("-_.")
    debug(f"moof: {truncated_name=}")
    return truncated_name


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
    application_docker_tag: str | None
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

    @field_validator("application_security_group_id")
    def validate_sec_group_id(application_security_group_id: Output[str]):  # noqa: N805
        return Output.from_input(application_security_group_id)

    @field_validator("application_security_group_name")
    def validate_sec_group_name(application_security_group_name: Output[str]):  # noqa: N805
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
        resource_options = ResourceOptions(parent=self).merge(opts)
        self.ol_app_k8s_config: OLApplicationK8sConfiguration = ol_app_k8s_config
        self.application_lb_service_name: str = (
            self.ol_app_k8s_config.application_lb_service_name
        )
        self.application_lb_service_port_name: str = (
            self.ol_app_k8s_config.application_lb_service_port_name
        )

        if ol_app_k8s_config.import_nginx_config:
            application_nginx_configmap = kubernetes.core.v1.ConfigMap(
                f"{self.ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-nginx-configmap",
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
        for k, v in (self.ol_app_k8s_config.application_config).items():
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

        init_containers = []
        if self.ol_app_k8s_config.init_collectstatic:
            init_containers.append(
                # Run database migrations at startup
                kubernetes.core.v1.ContainerArgs(
                    name="migrate",
                    image=f"mitodl/{self.ol_app_k8s_config.application_name}-application-app-main:{self.ol_app_k8s_config.application_docker_tag}",
                    command=["python3", "manage.py", "migrate", "--noinput"],
                    image_pull_policy="IfNotPresent",
                    env=application_deployment_env_vars,
                    env_from=application_deployment_envfrom,
                )
            )

        if self.ol_app_k8s_config.init_migrations:
            init_containers.append(
                kubernetes.core.v1.ContainerArgs(
                    name="collectstatic",
                    image=f"mitodl/{self.ol_app_k8s_config.application_name}-application-app-main:{self.ol_app_k8s_config.application_docker_tag}",
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
        application_labels = self.ol_app_k8s_config.k8s_global_labels | {
            "ol.mit.edu/application": f"{self.ol_app_k8s_config.application_name}-application",  # noqa: E501
            "ol.mit.edu/pod-security-group": self.ol_app_k8s_config.application_security_group_name.apply(  # noqa: E501
                truncate_k8s_metanames
            ),
        }

        _application_deployment = kubernetes.apps.v1.Deployment(
            f"{self.ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-deployment",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=truncate_k8s_metanames(
                    f"{self.ol_app_k8s_config.application_name}-app"
                ),
                namespace=self.ol_app_k8s_config.application_namespace,
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
                                    requests=self.ol_app_k8s_config.resource_requests,
                                    limits=self.ol_app_k8s_config.resource_limits,
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
                                name=f"{self.ol_app_k8s_config.application_name}-app",
                                image=f"mitodl/{self.ol_app_k8s_config.application_name}-application-app-main:{self.ol_app_k8s_config.application_docker_tag}",
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
            f"{self.ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-service",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=truncate_k8s_metanames(
                    self.ol_app_k8s_config.application_lb_service_name
                ),
                namespace=self.ol_app_k8s_config.application_namespace,
                labels=self.ol_app_k8s_config.k8s_global_labels,
            ),
            spec=kubernetes.core.v1.ServiceSpecArgs(
                selector=application_labels,
                ports=[
                    kubernetes.core.v1.ServicePortArgs(
                        name=self.ol_app_k8s_config.application_lb_service_name,
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
                f"{self.ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-application-pod-security-group-policy",
                api_version="vpcresources.k8s.aws/v1beta1",
                kind="SecurityGroupPolicy",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=self.ol_app_k8s_config.application_security_group_name.apply(
                        truncate_k8s_metanames
                    ),
                    namespace=self.ol_app_k8s_config.application_namespace,
                    labels=self.ol_app_k8s_config.k8s_global_labels,
                ),
                spec={
                    "podSelector": {
                        "matchLabels": {
                            "ol.mit.edu/pod-security-group": self.ol_app_k8s_config.application_security_group_name.apply(  # noqa: E501
                                truncate_k8s_metanames
                            ),
                        },
                    },
                    "securityGroups": {
                        "groupIds": [
                            self.ol_app_k8s_config.application_security_group_id,
                        ],
                    },
                },
            ),
        )
