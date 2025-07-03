# ruff: noqa: ERA001, E501
"""
This is a service components that replaces a number of "boilerplate" kubernetes
calls we currently make into one convenient callable package.
"""

from pathlib import Path
from typing import Any, Literal

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
    DEFAULT_REDIS_PORT,
    DEFAULT_UWSGI_PORT,
    MAXIMUM_K8S_NAME_LENGTH,
)
from bridge.lib.versions import NGINX_VERSION
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


def truncate_k8s_metanames(name: str) -> str:
    return name[:MAXIMUM_K8S_NAME_LENGTH].rstrip("-_.")


class OLApplicationK8sCeleryWorkerConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    queue_name: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL"] = (
        "INFO"
    )
    queues: list[str] = ["default"]
    resource_requests: dict[str, str] = Field(default={"cpu": "500m", "memory": "1Gi"})
    resource_limits: dict[str, str] = Field(
        default={"cpu": "2000m", "memory": "8Gi"},
    )
    min_replicas: NonNegativeInt = 1
    max_replicas: NonNegativeInt = 10
    autoscale_queue_depth: NonNegativeInt = 10
    redis_database_index: str = "1"
    redis_host: Output[str]
    redis_password: str
    redis_port: int = DEFAULT_REDIS_PORT


class OLApplicationK8sConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_root: Path
    application_config: dict[str, Any]
    application_name: str
    application_namespace: str
    application_lb_service_name: str
    application_lb_service_port_name: str
    application_min_replicas: NonNegativeInt = 2
    application_max_replicas: NonNegativeInt = 10
    k8s_global_labels: dict[str, str]
    env_from_secret_names: list[str]
    application_security_group_id: Output[str]
    application_security_group_name: Output[str]
    application_service_account_name: str | Output[str] | None = None
    application_image_repository: str
    application_image_repository_suffix: str | None = None
    application_docker_tag: str
    application_cmd_array: list[str] | None = None
    vault_k8s_resource_auth_name: str
    use_pullthrough_cache: bool = True
    image_pull_policy: str = "IfNotPresent"
    # You get CPU and memory autoscaling by default
    hpa_scaling_metrics: list[kubernetes.autoscaling.v2.MetricSpecArgs] = [
        kubernetes.autoscaling.v2.MetricSpecArgs(
            type="Resource",
            resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                name="cpu",
                target=kubernetes.autoscaling.v2.MetricTargetArgs(
                    type="Utilization",
                    average_utilization=80,  # Target CPU utilization (80%)
                ),
            ),
        ),
        kubernetes.autoscaling.v2.MetricSpecArgs(
            type="Resource",
            resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                name="memory",  # Memory utilization as the scaling metric
                target=kubernetes.autoscaling.v2.MetricTargetArgs(
                    type="Utilization",
                    average_utilization=80,  # Target memory utilization (80%)
                ),
            ),
        ),
    ]
    import_nginx_config: bool = Field(default=True)
    import_uwsgi_config: bool = Field(default=False)
    resource_requests: dict[str, str] = Field(
        default={"cpu": "250m", "memory": "300Mi"}
    )
    resource_limits: dict[str, str] = Field(
        default={"cpu": "2", "memory": "1500Mi"},
    )
    init_migrations: bool = Field(default=True)
    init_collectstatic: bool = Field(default=True)
    celery_worker_configs: list[OLApplicationK8sCeleryWorkerConfig] = []
    probe_configs: dict[str, kubernetes.core.v1.ProbeArgs] = {
        # Liveness probe to check if the application is still running
        "liveness_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/health/liveness/",
                port=DEFAULT_NGINX_PORT,
            ),
            initial_delay_seconds=30,  # Wait 30 seconds before first probe
            period_seconds=30,
            failure_threshold=3,  # Consider failed after 3 attempts
        ),
        # Readiness probe to check if the application is ready to serve traffic
        "readiness_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/health/readiness/",
                port=DEFAULT_NGINX_PORT,
            ),
            initial_delay_seconds=15,  # Wait 15 seconds before first probe
            period_seconds=15,
            failure_threshold=3,  # Consider failed after 3 attempts
        ),
        # Startup probe to ensure the application is fully initialized before other probes start
        "startup_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/health/startup/",
                port=DEFAULT_NGINX_PORT,
            ),
            initial_delay_seconds=10,  # Wait 10 seconds before first probe
            period_seconds=10,  # Probe every 10 seconds
            failure_threshold=30,  # Allow up to 5 minutes (30 * 10s) for startup
            success_threshold=1,
            timeout_seconds=5,
        ),
    }

    # See https://www.pulumi.com/docs/reference/pkg/python/pulumi/#pulumi.Output.from_input
    # for docs. This unwraps the value so Pydantic can store it in the config class.
    @field_validator("application_security_group_id")
    @classmethod
    def validate_sec_group_id(cls, application_security_group_id: Output[str]):
        """Ensure that the security group ID is unwrapped from the Pulumi Output."""
        return Output.from_input(application_security_group_id)

    @field_validator("application_security_group_name")
    @classmethod
    def validate_sec_group_name(cls, application_security_group_name: Output[str]):
        """Ensure that the security group name is unwrapped from the Pulumi Output."""
        return Output.from_input(application_security_group_name)

    @field_validator("application_config")
    @classmethod
    def validate_application_config(cls, application_config: dict[str, Any]):
        """Ensure that all application config values are strings."""
        # Convert all values to strings because that is what k8s expects.
        return {key: str(value) for key, value in application_config.items()}


stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"


class OLApplicationK8s(ComponentResource):
    """
    Main K8s component resource class
    """

    def __init__(  # noqa: C901, PLR0912
        self,
        ol_app_k8s_config: OLApplicationK8sConfig,
        opts: ResourceOptions | None = None,
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

        extra_deployment_args: dict[str, int] = {}
        # If we have ANY metrics args, then we use HPA and don't pass replicas to the deployment
        # If we don't have metrics, then we pass min_replicas to the deployment
        if ol_app_k8s_config.hpa_scaling_metrics:
            extra_deployment_args = {}
        else:
            extra_deployment_args = {
                "replicas": ol_app_k8s_config.application_min_replicas,
            }

        self.application_lb_service_name: str = (
            ol_app_k8s_config.application_lb_service_name
        )
        self.application_lb_service_port_name: str = (
            ol_app_k8s_config.application_lb_service_port_name
        )

        # Determine the full name of the container image
        if ol_app_k8s_config.application_image_repository_suffix:
            app_image = f"{ol_app_k8s_config.application_image_repository}{ol_app_k8s_config.application_image_repository_suffix}:{ol_app_k8s_config.application_docker_tag}"
        else:
            app_image = f"{ol_app_k8s_config.application_image_repository}:{ol_app_k8s_config.application_docker_tag}"
        if ol_app_k8s_config.use_pullthrough_cache:
            app_image = (
                f"610119931565.dkr.ecr.us-east-1.amazonaws.com/dockerhub/{app_image}"
            )

        volumes = [
            kubernetes.core.v1.VolumeArgs(
                name="staticfiles",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            )
        ]
        nginx_volume_mounts = [
            kubernetes.core.v1.VolumeMountArgs(
                name="staticfiles",
                mount_path="/src/staticfiles",
            )
        ]
        webapp_volume_mounts = nginx_volume_mounts.copy()

        # Import nginx configuration as a configmap
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
            volumes.append(
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
                )
            )
            nginx_volume_mounts.append(
                kubernetes.core.v1.VolumeMountArgs(
                    name="nginx-config",
                    mount_path="/etc/nginx/conf.d/web.conf",
                    sub_path="web.conf",
                    read_only=True,
                )
            )

        # Import uwsgi configuration as a configmap
        if ol_app_k8s_config.import_uwsgi_config:
            application_uwsgi_configmap = kubernetes.core.v1.ConfigMap(
                f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-uwsgi-configmap",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name="uwsgi-config",
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=ol_app_k8s_config.k8s_global_labels,
                ),
                data={
                    "uwsgi.ini": ol_app_k8s_config.project_root.joinpath(
                        "files/uwsgi.ini"
                    ).read_text(),
                },
                opts=resource_options,
            )
            volumes.append(
                kubernetes.core.v1.VolumeArgs(
                    name="uwsgi-config",
                    config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                        name=application_uwsgi_configmap.metadata.name,
                        items=[
                            kubernetes.core.v1.KeyToPathArgs(
                                key="uwsgi.ini",
                                path="uwsgi.ini",
                            ),
                        ],
                    ),
                )
            )
            webapp_volume_mounts.append(
                kubernetes.core.v1.VolumeMountArgs(
                    name="uwsgi-config",
                    mount_path="/tmp/uwsgi.ini",  # noqa: S108
                    sub_path="uwsgi.ini",
                    read_only=True,
                )
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
        application_deployment_envfrom = []
        for secret_name in ol_app_k8s_config.env_from_secret_names:
            application_deployment_envfrom.append(  # noqa: PERF401
                kubernetes.core.v1.EnvFromSourceArgs(
                    secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                        name=secret_name,
                    ),
                )
            )

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
            "ol.mit.edu/service": "webapp",
            "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
            "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                truncate_k8s_metanames
            ),
        }

        image_pull_policy = ol_app_k8s_config.image_pull_policy
        if ol_app_k8s_config.application_docker_tag.lower() == "latest":
            image_pull_policy = "Always"

        _application_deployment_name = truncate_k8s_metanames(
            f"{ol_app_k8s_config.application_name}-app"
        )
        _application_deployment = kubernetes.apps.v1.Deployment(
            f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-deployment",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=_application_deployment_name,
                namespace=ol_app_k8s_config.application_namespace,
                labels=application_labels,
            ),
            spec=kubernetes.apps.v1.DeploymentSpecArgs(
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
                        volumes=volumes,
                        init_containers=[
                            *init_containers,  # Add existing init containers after the new one
                        ],
                        dns_policy="ClusterFirst",
                        service_account_name=ol_app_k8s_config.application_service_account_name,
                        containers=[
                            # nginx container infront of uwsgi
                            kubernetes.core.v1.ContainerArgs(
                                name="nginx",
                                image=f"nginx:{NGINX_VERSION}",
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
                                volume_mounts=nginx_volume_mounts,
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
                                image_pull_policy=image_pull_policy,
                                resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                    requests=ol_app_k8s_config.resource_requests,
                                    limits=ol_app_k8s_config.resource_limits,
                                ),
                                command=ol_app_k8s_config.application_cmd_array,
                                env=application_deployment_env_vars,
                                env_from=application_deployment_envfrom,
                                volume_mounts=webapp_volume_mounts,
                                **ol_app_k8s_config.probe_configs,
                            ),
                        ],
                    ),
                ),
                **extra_deployment_args,
            ),
            opts=resource_options,
        )

        _application_hpa = kubernetes.autoscaling.v2.HorizontalPodAutoscaler(
            "application-hpa",
            spec=kubernetes.autoscaling.v2.HorizontalPodAutoscalerSpecArgs(
                scale_target_ref=kubernetes.autoscaling.v2.CrossVersionObjectReferenceArgs(
                    api_version="apps/v1",
                    kind="Deployment",
                    name=truncate_k8s_metanames(
                        f"{ol_app_k8s_config.application_name}-app"
                    ),
                ),
                min_replicas=ol_app_k8s_config.application_min_replicas,  # Minimum number of replicas
                max_replicas=ol_app_k8s_config.application_max_replicas,  # Maximum number of replicas
                # Corrected parameter name from "metrics" to the proper name for the API
                metrics=ol_app_k8s_config.hpa_scaling_metrics,
                # Optional: behavior configuration for scaling
                behavior=kubernetes.autoscaling.v2.HorizontalPodAutoscalerBehaviorArgs(
                    scale_up=kubernetes.autoscaling.v2.HPAScalingRulesArgs(
                        stabilization_window_seconds=60,  # Wait 1 minute before scaling up again
                        select_policy="Max",  # Choose max value when multiple metrics
                        policies=[
                            kubernetes.autoscaling.v2.HPAScalingPolicyArgs(
                                type="Percent",
                                value=100,  # Double pods at most
                                period_seconds=60,  # In this time period
                            )
                        ],
                    ),
                    scale_down=kubernetes.autoscaling.v2.HPAScalingRulesArgs(
                        stabilization_window_seconds=300,  # Wait 5 minutes before scaling down
                        select_policy="Min",
                        policies=[
                            kubernetes.autoscaling.v2.HPAScalingPolicyArgs(
                                type="Percent",
                                value=25,  # Remove at most 25% of pods at once
                                period_seconds=60,
                            )
                        ],
                    ),
                ),
            ),
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                namespace=ol_app_k8s_config.application_namespace,
            ),
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

        for celery_worker_config in ol_app_k8s_config.celery_worker_configs:
            celery_labels = ol_app_k8s_config.k8s_global_labels | {
                "ol.mit.edu/service": "celery",
                "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
                "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                    truncate_k8s_metanames
                ),
                # This is important!
                # Every type of worker needs a unique set of labels or the pod selectors will break.
                "ol.mit.edu/worker-name": celery_worker_config.queue_name,
            }

            _celery_deployment_name = truncate_k8s_metanames(
                f"{ol_app_k8s_config.application_name}-{celery_worker_config.queue_name}-celery-worker".replace(
                    "_", "-"
                )
            )
            _celery_deployment = kubernetes.apps.v1.Deployment(
                f"{ol_app_k8s_config.application_name}-celery-worker-{celery_worker_config.queue_name}-{stack_info.env_suffix}",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=_celery_deployment_name,
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=celery_labels,
                ),
                spec=kubernetes.apps.v1.DeploymentSpecArgs(
                    selector=kubernetes.meta.v1.LabelSelectorArgs(
                        match_labels=celery_labels,
                    ),
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels=celery_labels,
                        ),
                        # Ref: https://docs.celeryq.dev/en/stable/reference/cli.html#celery-worker
                        spec=kubernetes.core.v1.PodSpecArgs(
                            service_account_name=ol_app_k8s_config.application_service_account_name,
                            dns_policy="ClusterFirst",
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="celery-worker",
                                    image=app_image,
                                    command=[
                                        "celery",
                                        "-A",  # Application name
                                        "main.celery:app",
                                        "worker",  # COMMAND
                                        "-E",  # send task-related events for monitoring
                                        "-Q",  # queue name
                                        celery_worker_config.queue_name,
                                        "-B",  # beat (scheduler?)
                                        "-l",  # set log level
                                        celery_worker_config.log_level,
                                        "--max-tasks-per-child",  # Max number of tasks the pool worker will process before being replaced
                                        "100",
                                    ],
                                    env=application_deployment_env_vars,
                                    env_from=application_deployment_envfrom,
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests=celery_worker_config.resource_requests,
                                        limits=celery_worker_config.resource_limits,
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
                opts=resource_options,
            )

            _celery_scaled_object = kubernetes.apiextensions.CustomResource(
                f"{ol_app_k8s_config.application_name}-celery-worker-{celery_worker_config.queue_name}-{stack_info.env_suffix}-scaledobject",
                api_version="keda.sh/v1alpha1",
                kind="ScaledObject",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=_celery_deployment_name,
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=celery_labels,
                ),
                spec=Output.all(
                    deployment_name=_celery_deployment_name,
                    celery_config=celery_worker_config,
                    redis_host=celery_worker_config.redis_host,
                ).apply(
                    lambda deployment_info: {
                        "scaleTargetRef": {
                            "kind": "Deployment",
                            "name": deployment_info["deployment_name"],
                        },
                        "pollingInterval": 3,
                        "cooldownPeriod": 10,
                        "maxReplicaCount": deployment_info[
                            "celery_config"
                        ].max_replicas,
                        "minReplicaCount": deployment_info[
                            "celery_config"
                        ].min_replicas,
                        "triggers": [
                            {
                                "type": "redis",
                                "metadata": {
                                    "address": f"{deployment_info['redis_host']}:{deployment_info['celery_config'].redis_port}",
                                    "username": "default",
                                    "databaseIndex": deployment_info[
                                        "celery_config"
                                    ].redis_database_index,
                                    "password": deployment_info[
                                        "celery_config"
                                    ].redis_password,
                                    "listName": deployment_info[
                                        "celery_config"
                                    ].queue_name,
                                    "listLength": str(
                                        deployment_info[
                                            "celery_config"
                                        ].autoscale_queue_depth
                                    ),
                                    "enableTLS": "true",
                                },
                            },
                        ],
                    }
                ),
                opts=resource_options.merge(
                    ResourceOptions(delete_before_replace=True)
                ),
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
    secret_ref: str | None = Field(
        None,
        alias="secretRef",
    )
    config: dict[str, Any] = {}


class OLApisixRouteConfig(BaseModel):
    route_name: str
    priority: int = 0
    shared_plugin_config_name: str | None = None
    plugins: list[OLApisixPluginConfig] = []
    hosts: list[str] = []
    paths: list[str] = []
    backend_service_name: str | None = None
    backend_service_port: str | None = None
    # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/#service-resolution-granularity
    backend_resolve_granularity: Literal["endpoint", "service"] = "service"
    upstream: str | None = None
    websocket: bool = False

    @field_validator("plugins")
    @classmethod
    def ensure_request_id_plugin(
        cls, v: list[OLApisixPluginConfig]
    ) -> list[OLApisixPluginConfig]:
        """
        Ensure that the request-id plugin is always added to the plugins list
        """
        if not any(plugin.name == "request-id" for plugin in v):
            v.append(
                OLApisixPluginConfig(
                    name="request-id", config={"include_in_response": True}
                )
            )
        return v

    @model_validator(mode="after")
    def check_backend_or_upstream(self) -> "OLApisixRouteConfig":
        """Ensure that either upstream or backend service details are provided, not both."""
        upstream: str | None = self.upstream
        backend_service_name: str | None = self.backend_service_name
        backend_service_port: str | None = self.backend_service_port

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
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Initialize the OLApisixRoute component resource."""
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
    oidc_renew_access_token_on_expiry: bool = True
    oidc_scope: str = "openid profile email"
    oidc_session_contents: dict[str, bool] = {
        "access_token": True,
        "enc_id_token": True,
        "id_token": True,
        "user": True,
    }
    oidc_session_cookie_domain: str | None = None
    oidc_session_cookie_lifetime: NonNegativeInt = 0
    oidc_ssl_verify: bool = True
    oidc_use_session_secret: bool = True


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
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Initialize the OLApisixOIDCResources component resource."""
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
            __templates["session.secret"] = '{{ get .Secrets "secret" }}'

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

        cookie_config = {}
        if oidc_config.oidc_session_cookie_domain:
            cookie_config["session"] = {
                "cookie": {"domain": oidc_config.oidc_session_cookie_domain}
            }

        self.base_oidc_config = {
            "scope": oidc_config.oidc_scope,
            "bearer_only": oidc_config.oidc_bearer_only,
            "introspection_endpoint_auth_method": oidc_config.oidc_introspection_endpoint_auth_method,
            "ssl_verify": oidc_config.oidc_ssl_verify,
            "renew_access_token_on_expiry": oidc_config.oidc_renew_access_token_on_expiry,
            "logout_path": oidc_config.oidc_logout_path,
            "post_logout_redirect_uri": oidc_config.oidc_post_logout_redirect_uri,
            **cookie_config,
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
        """Return the base OIDC configuration dictionary."""
        return {
            **self.base_oidc_config,
            "unauth_action": unauth_action,
        }

    def get_full_oidc_plugin_config(self, unauth_action: str) -> dict[str, Any]:
        """Return the full OIDC plugin configuration dictionary for Apisix."""
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
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Initialize the OLApisixSharedPlugins component resource."""
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
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Initialize the OLApisixExternalUpstream component resource."""
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


class OLTraefikMiddleware(pulumi.ComponentResource):
    """
    Generic component for creating Traefik Middleware custom resources.
    """

    def __init__(
        self,
        name: str,
        middleware_name: str,
        namespace: str,
        spec: dict[str, Any],
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Initialize the OLTraefikMiddleware component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLTraefikMiddleware", name, None, opts
        )
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        self.traefik_middleware = kubernetes.apiextensions.CustomResource(
            f"OLTraefikMiddleware-{name}",
            api_version="traefik.io/v1alpha1",
            kind="Middleware",
            metadata={
                "name": middleware_name,
                "namespace": namespace,
            },
            spec=spec,
            opts=resource_options,
        )
        self.gateway_filter = {
            "type": "ExtensionRef",
            "extensionRef": {
                "group": "traefik.io",
                "kind": "Middleware",
                "name": middleware_name,
            },
        }
