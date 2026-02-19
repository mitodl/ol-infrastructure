# ruff: noqa: ERA001, E501
"""
This is a service components that replaces a number of "boilerplate" kubernetes
calls we currently make into one convenient callable package.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pulumi_kubernetes as kubernetes
import pulumiverse_time as pulumi_time
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
    DEFAULT_WSGI_PORT,
    MAXIMUM_K8S_NAME_LENGTH,
)
from bridge.lib.versions import NGINX_VERSION
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri, ecr_image_uri
from ol_infrastructure.lib.ol_types import Component, KubernetesServiceAppProtocol
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
    resource_requests: dict[str, str] = Field(default={"cpu": "250m", "memory": "2Gi"})
    resource_limits: dict[str, str] = Field(
        default={"memory": "2Gi"},
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
    application_lb_service_app_protocol: KubernetesServiceAppProtocol | None = (
        None  # Optional hint for gateway controllers (e.g., WSS for WebSocket over TLS)
    )
    application_min_replicas: NonNegativeInt = 2
    application_max_replicas: NonNegativeInt = 10
    application_deployment_use_anti_affinity: bool = True
    k8s_global_labels: dict[str, str]
    env_from_secret_names: list[str]
    application_security_group_id: Output[str]
    application_security_group_name: Output[str]
    application_service_account_name: str | Output[str] | None = None
    application_image_repository: str
    application_image_repository_suffix: str | None = None
    application_docker_tag: str | None = None
    application_image_digest: str | None = None
    application_cmd_array: list[str] | None = None
    application_arg_array: list[str] | None = None
    deployment_notifications: bool = False
    slack_channel: str | None = None  # Slack channel for deployment notifications
    vault_k8s_resource_auth_name: str
    registry: Literal["dockerhub", "ecr"] = "ecr"
    image_pull_policy: str = "IfNotPresent"
    # You get CPU and memory autoscaling by default
    hpa_scaling_metrics: list[kubernetes.autoscaling.v2.MetricSpecArgs] = [
        kubernetes.autoscaling.v2.MetricSpecArgs(
            type="Resource",
            resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                name="cpu",
                target=kubernetes.autoscaling.v2.MetricTargetArgs(
                    type="Utilization",
                    average_utilization=60,  # Target CPU utilization (60%)
                ),
            ),
        ),
        kubernetes.autoscaling.v2.MetricSpecArgs(
            type="Resource",
            resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                name="memory",  # Memory utilization as the scaling metric
                target=kubernetes.autoscaling.v2.MetricTargetArgs(
                    type="Utilization",
                    average_utilization=80,  # Target memory utilization (60%)
                ),
            ),
        ),
    ]
    import_nginx_config: bool = Field(default=True)
    import_nginx_config_path: str = "files/web.conf"
    import_uwsgi_config: bool = Field(default=False)
    application_port: int | None = (
        None  # Override container/service port (default: 8073 with nginx, else 8040)
    )
    resource_requests: dict[str, str] = Field(default={"cpu": "250m", "memory": "1Gi"})
    resource_limits: dict[str, str] = Field(default={"memory": "1Gi"})
    init_migrations: bool = Field(default=True)
    init_collectstatic: bool = Field(default=True)
    celery_worker_configs: list[OLApplicationK8sCeleryWorkerConfig] = []
    pre_deploy_commands: list[tuple[str, list[str]]] | None = Field(
        default=None,
        description="A tuple of <job_name>, <job_command_array> for executing prior to the deployment updating",
    )
    post_deploy_commands: list[tuple[str, list[str]]] | None = Field(
        default=None,
        description="A tuple of <job_name>, <job_command_array> for executing upon completion of the deployment updating",
    )
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
            timeout_seconds=3,
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
            timeout_seconds=3,
        ),
        # Startup probe to ensure the application is fully initialized before other probes start
        "startup_probe": kubernetes.core.v1.ProbeArgs(
            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                path="/health/startup/",
                port=DEFAULT_NGINX_PORT,
            ),
            initial_delay_seconds=10,  # Wait 10 seconds before first probe
            period_seconds=10,  # Probe every 10 seconds
            failure_threshold=6,  # Allow up to 1 minutes (6 * 10s) for startup
            success_threshold=1,
            timeout_seconds=5,
        ),
    }
    app_pdb_maximum_unavailable: NonNegativeInt | str = 1

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

    @model_validator(mode="after")
    def validate_image_tag_or_digest(self):
        """Ensure that exactly one of application_docker_tag or application_image_digest is provided."""
        if self.application_docker_tag and self.application_image_digest:
            msg = "Cannot specify both application_docker_tag and application_image_digest"
            raise ValueError(msg)
        if not self.application_docker_tag and not self.application_image_digest:
            msg = (
                "Must specify either application_docker_tag or application_image_digest"
            )
            raise ValueError(msg)
        return self


stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"


class OLApplicationK8s(ComponentResource):
    """
    Main K8s component resource class
    """

    def __init__(  # noqa: C901, PLR0912, PLR0915
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
        deployment_options = ResourceOptions(parent=self)

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

        # Determine the application port to use
        # If not specified, use DEFAULT_NGINX_PORT (8071) if nginx is enabled, else DEFAULT_WSGI_PORT (8073)
        if ol_app_k8s_config.application_port is not None:
            application_port = ol_app_k8s_config.application_port
        elif ol_app_k8s_config.import_nginx_config:
            application_port = DEFAULT_NGINX_PORT
        else:
            application_port = DEFAULT_WSGI_PORT

        # Determine the full name of the container image
        if ol_app_k8s_config.application_image_digest:
            # Use digest format: repository@sha256:digest
            if ol_app_k8s_config.application_image_repository_suffix:
                app_image = f"{ol_app_k8s_config.application_image_repository}{ol_app_k8s_config.application_image_repository_suffix}@{ol_app_k8s_config.application_image_digest}"
            else:
                app_image = f"{ol_app_k8s_config.application_image_repository}@{ol_app_k8s_config.application_image_digest}"
        elif ol_app_k8s_config.application_image_repository_suffix:
            # Use tag format: repository:tag with suffix
            app_image = f"{ol_app_k8s_config.application_image_repository}{ol_app_k8s_config.application_image_repository_suffix}:{ol_app_k8s_config.application_docker_tag}"
        else:
            # Use tag format: repository:tag
            app_image = f"{ol_app_k8s_config.application_image_repository}:{ol_app_k8s_config.application_docker_tag}"
        if ol_app_k8s_config.registry == "dockerhub":
            app_image = cached_image_uri(app_image)
        else:
            app_image = ecr_image_uri(app_image)

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

        app_containers = []
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
                        ol_app_k8s_config.import_nginx_config_path
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
            app_containers.append(  # nginx container infront of uwsgi
                kubernetes.core.v1.ContainerArgs(
                    name="nginx",
                    image=cached_image_uri(f"nginx:{NGINX_VERSION}"),
                    ports=[
                        kubernetes.core.v1.ContainerPortArgs(
                            container_port=DEFAULT_NGINX_PORT
                        )
                    ],
                    image_pull_policy="IfNotPresent",
                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                        requests={"cpu": "50m", "memory": "50Mi"},
                        limits={"cpu": "50m", "memory": "50Mi"},
                    ),
                    volume_mounts=nginx_volume_mounts,
                ),
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
            kubernetes.core.v1.EnvVarArgs(name="PORT", value=str(DEFAULT_WSGI_PORT))
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

        image_pull_policy = ol_app_k8s_config.image_pull_policy
        if (
            ol_app_k8s_config.application_docker_tag
            and ol_app_k8s_config.application_docker_tag.lower() == "latest"
        ):
            image_pull_policy = "Always"

        init_containers = []
        if ol_app_k8s_config.init_migrations:
            init_containers.append(
                # Run database migrations at startup
                kubernetes.core.v1.ContainerArgs(
                    name="migrate",
                    image=app_image,
                    command=["python3", "manage.py", "migrate", "--noinput"],
                    image_pull_policy=image_pull_policy,
                    env=application_deployment_env_vars,
                    env_from=application_deployment_envfrom,
                )
            )

        if ol_app_k8s_config.init_collectstatic:
            init_containers.append(
                kubernetes.core.v1.ContainerArgs(
                    name="collectstatic",
                    image=app_image,
                    command=["python3", "manage.py", "collectstatic", "--noinput"],
                    image_pull_policy=image_pull_policy,
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
            "ol.mit.edu/process": "webapp",
            "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
            "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                truncate_k8s_metanames
            ),
        }

        # Add deployment notification label if enabled
        if ol_app_k8s_config.deployment_notifications:
            application_labels["ol.mit.edu/notify-deployments"] = "true"

        # Add Slack channel label if specified
        if ol_app_k8s_config.slack_channel:
            application_labels["ol.mit.edu/slack-channel"] = (
                ol_app_k8s_config.slack_channel
            )

        pod_spec_args = {}
        if ol_app_k8s_config.application_deployment_use_anti_affinity:
            pod_spec_args["affinity"] = kubernetes.core.v1.AffinityArgs(
                pod_anti_affinity=kubernetes.core.v1.PodAntiAffinityArgs(
                    preferred_during_scheduling_ignored_during_execution=[
                        kubernetes.core.v1.WeightedPodAffinityTermArgs(
                            weight=100,
                            pod_affinity_term=kubernetes.core.v1.PodAffinityTermArgs(
                                label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                                    match_labels=application_labels,
                                ),
                                topology_key="kubernetes.io/hostname",
                            ),
                        ),
                    ],
                ),
            )

        _application_deployment_name = truncate_k8s_metanames(
            f"{ol_app_k8s_config.application_name}-app"
        )

        if pre_deploy_commands := ol_app_k8s_config.pre_deploy_commands:
            _pre_deploy_job = kubernetes.batch.v1.Job(
                f"{ol_app_k8s_config.application_name}-{stack_info.env_suffix}-pre-deploy-job",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=f"{_application_deployment_name}-pre-deploy",
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=ol_app_k8s_config.k8s_global_labels
                    | {
                        "ol.mit.edu/job": "pre-deploy",
                        "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
                        "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                            truncate_k8s_metanames
                        ),
                    },
                ),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # Remove job 30 minutes after completion
                    ttl_seconds_after_finished=60 * 30,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels=application_labels,
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            service_account_name=ol_app_k8s_config.application_service_account_name,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name=command_name,
                                    image=app_image,
                                    command=command_array,
                                    image_pull_policy=image_pull_policy,
                                    env=application_deployment_env_vars,
                                    env_from=application_deployment_envfrom,
                                )
                                for (command_name, command_array) in pre_deploy_commands
                            ],
                            restart_policy="Never",
                        ),
                    ),
                ),
                opts=resource_options,
            )
            deployment_options = deployment_options.merge(
                ResourceOptions(depends_on=[_pre_deploy_job])
            )

            if ol_app_k8s_config.deployment_notifications:
                _application_pre_deployment_event_name = truncate_k8s_metanames(
                    f"{ol_app_k8s_config.application_name}-predeploy"
                )
                # Create pre-deployment event
                _now = datetime.now(tz=UTC)
                _pre_deployment_event = kubernetes.events.v1.Event(
                    f"{ol_app_k8s_config.application_name}-{stack_info.env_suffix}-pre-deploy-event",
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        name=_application_pre_deployment_event_name,
                        namespace=ol_app_k8s_config.application_namespace,
                        labels=application_labels,
                        deletion_timestamp=(_now + timedelta(seconds=30)).isoformat(),
                    ),
                    event_time=pulumi_time.Static(
                        f"{_application_pre_deployment_event_name}-time",
                        triggers={"pre_deploy_job_id": _pre_deploy_job.id},
                    ),
                    regarding=kubernetes.core.v1.ObjectReferenceArgs(
                        api_version="batch/v1",
                        kind="Job",
                        name=_application_pre_deployment_event_name,
                        namespace=ol_app_k8s_config.application_namespace,
                    ),
                    action="PreDeployJobStarted",
                    reason="PreDeployJobStarted",
                    note=f"Pre-deployment job started for {_application_deployment_name}",
                    type="Normal",
                    reporting_controller="ol-infrastructure",
                    reporting_instance=f"{ol_app_k8s_config.application_name}-controller",
                    opts=ResourceOptions(delete_before_replace=True).merge(
                        resource_options
                    ),
                )

        app_containers.append(
            # Actual application run with uwsgi
            kubernetes.core.v1.ContainerArgs(
                name=f"{ol_app_k8s_config.application_name}-app",
                image=app_image,
                ports=[
                    kubernetes.core.v1.ContainerPortArgs(
                        container_port=application_port
                    )
                ],
                image_pull_policy=image_pull_policy,
                resources=kubernetes.core.v1.ResourceRequirementsArgs(
                    requests=ol_app_k8s_config.resource_requests,
                    limits=ol_app_k8s_config.resource_limits,
                ),
                command=ol_app_k8s_config.application_cmd_array,
                args=ol_app_k8s_config.application_arg_array,
                env=application_deployment_env_vars,
                env_from=application_deployment_envfrom,
                volume_mounts=webapp_volume_mounts,
                **ol_app_k8s_config.probe_configs,
            ),
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
                        max_surge="100%",
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
                        containers=app_containers,
                        **pod_spec_args,
                    ),
                ),
                **extra_deployment_args,
            ),
            opts=deployment_options,
        )

        if post_deploy_commands := ol_app_k8s_config.post_deploy_commands:
            # Create post-deployment job
            _post_deploy_job = kubernetes.batch.v1.Job(
                f"{ol_app_k8s_config.application_name}-{stack_info.env_suffix}-post-deploy-job",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=f"{_application_deployment_name}-post-deploy",
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=ol_app_k8s_config.k8s_global_labels
                    | {
                        "ol.mit.edu/job": "post-deploy",
                        "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
                        "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                            truncate_k8s_metanames
                        ),
                    },
                ),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # Remove job 30 minutes after completion
                    ttl_seconds_after_finished=60 * 30,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels=application_labels,
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            service_account_name=ol_app_k8s_config.application_service_account_name,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name=command_name,
                                    image=app_image,
                                    command=command_array,
                                    image_pull_policy=image_pull_policy,
                                    env=application_deployment_env_vars,
                                    env_from=application_deployment_envfrom,
                                )
                                for (
                                    command_name,
                                    command_array,
                                ) in post_deploy_commands
                            ],
                            restart_policy="Never",
                        ),
                    ),
                ),
                opts=resource_options.merge(
                    ResourceOptions(depends_on=[_application_deployment])
                ),
            )

            if ol_app_k8s_config.deployment_notifications:
                # Create post-deployment event
                _application_post_deployment_event_name = truncate_k8s_metanames(
                    f"{ol_app_k8s_config.application_name}-postdeploy"
                )

                # Use the job's status to determine success/failure for the event
                def create_post_deploy_event_note(job_status):
                    """Create event note based on job completion status."""
                    if job_status is None:
                        return f"Post-deployment job started for {_application_deployment_name}"

                    succeeded = job_status.get("succeeded", 0)
                    failed = job_status.get("failed", 0)

                    if succeeded > 0:
                        return f"Post-deployment job completed successfully for {_application_deployment_name}"
                    elif failed > 0:
                        return f"Post-deployment job failed for {_application_deployment_name}"
                    else:
                        return f"Post-deployment job in progress for {_application_deployment_name}"

                def create_post_deploy_event_type(job_status):
                    """Determine event type based on job completion status."""
                    if job_status is None:
                        return "Normal"

                    failed = job_status.get("failed", 0)
                    return "Warning" if failed > 0 else "Normal"

                def create_post_deploy_event_action(job_status):
                    """Determine event action based on job completion status."""
                    if job_status is None:
                        return "PostDeployJobStarted"

                    succeeded = job_status.get("succeeded", 0)
                    failed = job_status.get("failed", 0)

                    if succeeded > 0:
                        return "PostDeployJobCompleted"
                    elif failed > 0:
                        return "PostDeployJobFailed"
                    else:
                        return "PostDeployJobInProgress"

                def create_post_deploy_event_reason(job_status):
                    """Determine event reason based on job completion status."""
                    if job_status is None:
                        return "PostDeployJobStarted"

                    succeeded = job_status.get("succeeded", 0)
                    failed = job_status.get("failed", 0)

                    if succeeded > 0:
                        return "PostDeployJobSucceeded"
                    elif failed > 0:
                        return "PostDeployJobFailed"
                    else:
                        return "PostDeployJobRunning"

                _now = datetime.now(tz=UTC)
                _post_deployment_event = kubernetes.events.v1.Event(
                    f"{ol_app_k8s_config.application_name}-{stack_info.env_suffix}-post-deploy-event",
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        name=_application_post_deployment_event_name,
                        namespace=ol_app_k8s_config.application_namespace,
                        labels=application_labels,
                        deletion_timestamp=(_now + timedelta(seconds=30)).isoformat(),
                    ),
                    event_time=pulumi_time.Static(
                        f"{_application_post_deployment_event_name}-time",
                        triggers={"post_deploy_job_id": _post_deploy_job.id},
                    ),
                    regarding=kubernetes.core.v1.ObjectReferenceArgs(
                        api_version="batch/v1",
                        kind="Job",
                        name=f"{_application_deployment_name}-post-deploy",
                        namespace=ol_app_k8s_config.application_namespace,
                    ),
                    action=_post_deploy_job.status.apply(
                        create_post_deploy_event_action
                    ),
                    reason=_post_deploy_job.status.apply(
                        create_post_deploy_event_reason
                    ),
                    note=_post_deploy_job.status.apply(create_post_deploy_event_note),
                    type=_post_deploy_job.status.apply(create_post_deploy_event_type),
                    reporting_controller="ol-infrastructure",
                    reporting_instance=f"{ol_app_k8s_config.application_name}-controller",
                    opts=resource_options.merge(
                        ResourceOptions(depends_on=[_post_deploy_job])
                    ),
                )

        # Pod Disruption Budget to ensure at least one web application pod is available.
        _application_pdb = kubernetes.policy.v1.PodDisruptionBudget(
            f"{ol_app_k8s_config.application_name}-application-{stack_info.env_suffix}-pdb",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=f"{_application_deployment_name}-pdb",
                namespace=ol_app_k8s_config.application_namespace,
                labels=application_labels,
            ),
            spec=kubernetes.policy.v1.PodDisruptionBudgetSpecArgs(
                max_unavailable=ol_app_k8s_config.app_pdb_maximum_unavailable,
                selector=kubernetes.meta.v1.LabelSelectorArgs(
                    match_labels=application_labels,
                ),
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
                        port=application_port,
                        target_port=application_port,
                        protocol="TCP",
                        app_protocol=ol_app_k8s_config.application_lb_service_app_protocol,
                    ),
                ],
                type="ClusterIP",
            ),
            opts=resource_options,
        )

        for celery_worker_config in ol_app_k8s_config.celery_worker_configs:
            celery_labels = ol_app_k8s_config.k8s_global_labels | {
                "ol.mit.edu/component": str(Component.celery),
                "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
                "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                    truncate_k8s_metanames
                ),
                # This is important!
                # Every type of worker needs a unique set of labels or the pod selectors will break.
                "ol.mit.edu/worker-name": celery_worker_config.queue_name,
            }

            # Add Slack channel label if specified
            if ol_app_k8s_config.slack_channel:
                celery_labels["ol.mit.edu/slack-channel"] = (
                    ol_app_k8s_config.slack_channel
                )

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
                                        "--scheduler",
                                        "redbeat.RedBeatScheduler",
                                        "-l",  # set log level
                                        celery_worker_config.log_level,
                                        "--max-tasks-per-child",  # Max number of tasks the pool worker will process before being replaced
                                        "100",
                                        "--concurrency=2",  # Don't try to use all cores on node
                                        "--prefetch-multiplier=1",
                                    ],
                                    env=[
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="CELERY_TASK_ACKS_LATE",
                                            value="True",
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="CELERY_TASK_REJECT_ON_WORKER_LOST",
                                            value="True",
                                        ),
                                        *application_deployment_env_vars,
                                    ],
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
                        "advanced": {
                            "horizontalPodAutoscalerConfig": {
                                "behavior": {
                                    "scaleUp": {
                                        "stabilizationWindowSeconds": 300,
                                    },
                                }
                            }
                        },
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
    backend_service_port: str | NonNegativeInt | None = None
    # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/#service-resolution-granularity
    backend_resolve_granularity: Literal["endpoint", "service"] = "service"
    upstream: str | None = None
    websocket: bool = False
    timeout_connect: str = "60s"
    timeout_read: str = "60s"
    timeout_send: str = "60s"

    @field_validator("timeout_connect", "timeout_read", "timeout_send")
    @classmethod
    def validate_timeout(cls, v: str) -> str:
        """Ensure that the timeout value is a non-negative integer followed by 's'."""
        if not v.endswith("s") or not v[:-1].isdigit() or int(v[:-1]) <= 0:
            msg = "Timeout must be a positive integer greater than 0 followed by 's' (e.g. '60s')"
            raise ValueError(msg)
        return v

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
        backend_service_port: str | NonNegativeInt | None = self.backend_service_port

        if upstream is not None:
            if backend_service_name is not None or backend_service_port is not None:
                msg = "If 'upstream' is provided, 'backend_service_name' and 'backend_service_port' must not be provided."
                raise ValueError(msg)
        elif backend_service_name is None or backend_service_port is None:
            msg = "If 'upstream' is not provided, both 'backend_service_name' and 'backend_service_port' must be provided."
            raise ValueError(msg)
        return self


class OLApisixRoute(ComponentResource):
    """
    Route configuration for apisix
    Defines and creates an "ApisixRoute" resource in the k8s cluster
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        route_configs: list[OLApisixRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        ingress_class_name: str = "apache-apisix",
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixRoute component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixRoute", name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

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
                "ingressClassName": ingress_class_name,
                "http": self.__build_route_list(route_configs),
            },
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
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
                "timeout": {
                    "connect": route_config.timeout_connect,
                    "send": route_config.timeout_send,
                    "read": route_config.timeout_read,
                },
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
    oidc_scope: str = "openid profile email organization:*"
    oidc_session_contents: dict[str, bool] = {
        "access_token": True,
        "enc_id_token": True,
        "id_token": True,
        "user": True,
    }
    oidc_session_cookie_chunks: NonNegativeInt = 0
    oidc_session_cookie_domain: str | None = None
    oidc_session_cookie_lifetime: NonNegativeInt = 0
    oidc_ssl_verify: bool = True
    oidc_use_session_secret: bool = True


class OLApisixOIDCResources(ComponentResource):
    """
    OIDC configuration for apisix
    Defines and creates an "OLVaultK8SSecret" resource in the k8s cluster
    Also provides helper functions for creating config blocks for the oidc plugin
    """

    def __init__(
        self,
        name: str,
        oidc_config: OLApisixOIDCConfig,
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixOIDCResources component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixOIDCResources", name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

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
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )

        cookie_config = {}
        _cookie: dict[str, Any] = {}
        if oidc_config.oidc_session_cookie_domain:
            _cookie["domain"] = oidc_config.oidc_session_cookie_domain
        if oidc_config.oidc_session_cookie_chunks:
            _cookie["chunks"] = oidc_config.oidc_session_cookie_chunks
        if _cookie:
            cookie_config["session"] = {"cookie": _cookie}

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


class OLApisixSharedPlugins(ComponentResource):
    """
    Shared plugin configuration for apisix
    Defines and creates an "ApisixPluginConfig" resource in the k8s cluster
    """

    def __init__(
        self,
        name: str,
        plugin_config: OLApisixSharedPluginsConfig,
        opts: ResourceOptions | None = None,
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
            {
                "name": "prometheus",
                "enable": True,
                "config": {"prefer_name": True},
            },
        ]

        resource_options = ResourceOptions(parent=self).merge(opts)

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


class OLApisixExternalUpstream(ComponentResource):
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
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixExternalUpstream component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixExternalUpstream", name, None, opts
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

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


class OLTraefikMiddleware(ComponentResource):
    """
    Generic component for creating Traefik Middleware custom resources.
    """

    def __init__(
        self,
        name: str,
        middleware_name: str,
        namespace: str,
        spec: dict[str, Any],
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLTraefikMiddleware component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLTraefikMiddleware", name, None, opts
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

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
