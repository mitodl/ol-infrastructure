# ruff: noqa: ERA001, E501
"""K8s application deployment components."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import pulumi_kubernetes as kubernetes
import pulumiverse_time as pulumi_time
from kubernetes.utils.quantity import parse_quantity
from pulumi import Alias, ComponentResource, CustomTimeouts, Output, ResourceOptions
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
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
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri, ecr_image_uri
from ol_infrastructure.lib.ol_types import Component, KubernetesServiceAppProtocol
from ol_infrastructure.lib.pulumi_helper import parse_stack


def truncate_k8s_metanames(name: str) -> str:
    return name[:MAXIMUM_K8S_NAME_LENGTH].rstrip("-_.")


class OLApplicationK8sCeleryWorkerConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    application_name: str = "main.celery:app"
    queue_name: str | None = None
    worker_name: str | None = None
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
    run_beat: bool = (
        False  # Deprecated: use celery_beat_config on OLApplicationK8sConfig instead
    )

    @field_validator("queue_name", "worker_name")
    @classmethod
    def validate_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            msg = "must be None or a non-empty, non-whitespace string"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def resolve_worker_name(self) -> "OLApplicationK8sCeleryWorkerConfig":
        """Derive worker_name from queue_name if not explicitly set.

        worker_name drives K8s resource names, pod labels, and the KEDA Redis
        listName.  queue_name drives the -Q CLI flag passed to the celery worker.
        Keeping them separate allows a worker to consume from all queues (no -Q)
        while still scaling on a specific Redis list.
        """
        if self.worker_name is None:
            if self.queue_name is None:
                msg = "At least one of 'queue_name' or 'worker_name' must be set"
                raise ValueError(msg)
            self.worker_name = self.queue_name
        return self


class OLApplicationK8sCeleryBeatConfig(BaseModel):
    """Configuration for a standalone celery beat scheduler deployment.

    Use this instead of run_beat=True on a worker. A dedicated beat deployment
    runs exactly one replica and is never autoscaled, ensuring only one scheduler
    writes to the RedBeat Redis keys at any time.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    application_name: str = "main.celery:app"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL"] = (
        "INFO"
    )
    resource_requests: dict[str, str] = Field(
        default={"cpu": "100m", "memory": "512Mi"}
    )
    resource_limits: dict[str, str] = Field(default={"memory": "512Mi"})
    scheduler: str = "redbeat.RedBeatScheduler"


class GranianConfig(BaseModel):
    """Configuration for running applications with the Granian ASGI/WSGI server.

    When set on OLApplicationK8sConfig, the component will generate the granian
    command/args, select the appropriate nginx config, expose a metrics port, and
    create a PodMonitor — replacing the manual if/else blocks in each caller.

    The supported subset of granian CLI options is: interface, host, port, workers,
    runtime_mode, runtime_threads, no_ws, workers_max_rss,
    blocking_threads_idle_timeout, respawn_failed_workers, backlog, log_level,
    application_module, and metrics-related flags.

    **Port/nginx coupling:** when ``import_nginx_config=True`` on
    ``OLApplicationK8sConfig``, the nginx config proxies to a fixed upstream address
    (typically ``127.0.0.1:{DEFAULT_WSGI_PORT}``). Overriding ``port`` without also
    supplying a custom ``nginx_config_filename`` whose content matches the new port
    will silently misdirect traffic. A cross-model validator on
    ``OLApplicationK8sConfig`` enforces this constraint at stack evaluation time.
    """

    interface: Literal["wsgi", "asgi", "asginl"] = "wsgi"
    host: str = "0.0.0.0"  # noqa: S104
    port: Annotated[int, Field(ge=1, le=65535)] = DEFAULT_WSGI_PORT
    workers: PositiveInt = 2
    runtime_mode: str | None = "mt"
    runtime_threads: PositiveInt = 2
    no_ws: bool = True
    limit_workers_max_rss: bool = True
    """When ``True`` (default), automatically cap each worker's RSS at 90 % of the
    per-worker share of the container memory limit.  Set to ``False`` to disable the
    ``--workers-max-rss`` flag entirely (e.g. for ASGI apps without a fixed memory
    budget or when the limit is managed externally)."""
    workers_max_rss: PositiveInt | None = None
    """Explicit per-worker RSS cap in MiB.  When ``None`` and ``limit_workers_max_rss``
    is ``True``, the value is derived from the container ``resource_limits["memory"]``
    via ``floor(memory_limit_bytes / workers * 0.9) MiB``.  Set explicitly only to
    override the computed value."""
    blocking_threads_idle_timeout: PositiveInt | None = None
    """Seconds before an idle blocking thread is retired (granian ``--blocking-threads-idle-timeout``). Omitted when ``None``."""
    respawn_failed_workers: bool = True
    backlog: PositiveInt | None = 128
    log_level: str = "warning"
    application_module: str = "main.wsgi:application"
    enable_metrics: bool = True
    metrics_port: Annotated[int, Field(ge=1, le=65535)] = 9090
    metrics_scrape_interval: PositiveInt = 30
    nginx_config_filename: str = "web.conf_granian"
    static_path_mounts: list[str] = Field(
        default_factory=list,
        description=(
            "Paths to mount as Granian static file directories. Each entry produces a "
            "'--static-path-mount <path>' argument pair. Granian supports multiple "
            "static path mounts simultaneously."
        ),
    )

    @field_validator("nginx_config_filename")
    @classmethod
    def validate_nginx_config_filename(cls, v: str) -> str:
        """Ensure nginx_config_filename is a simple filename with no path traversal."""
        p = Path(v)
        if p.name != v or ".." in p.parts:
            msg = (
                "granian_config.nginx_config_filename must be a plain filename "
                "with no path separators or '..' components"
            )
            raise ValueError(msg)
        return v

    def build_args(self) -> list[str]:
        """Build the granian CLI argument list from this configuration."""
        args = [
            "--interface",
            self.interface,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--workers",
            str(self.workers),
        ]
        if self.no_ws:
            args.append("--no-ws")
        if self.runtime_mode is not None:
            args += ["--runtime-mode", self.runtime_mode]
        args += ["--runtime-threads", str(self.runtime_threads)]
        if self.workers_max_rss is not None:
            args += ["--workers-max-rss", str(self.workers_max_rss)]
        if self.blocking_threads_idle_timeout is not None:
            args += [
                "--blocking-threads-idle-timeout",
                str(self.blocking_threads_idle_timeout),
            ]
        if self.respawn_failed_workers:
            args.append("--respawn-failed-workers")
        if self.backlog is not None:
            args += ["--backlog", str(self.backlog)]
        if self.enable_metrics:
            args += [
                "--metrics",
                "--metrics-scrape-interval",
                str(self.metrics_scrape_interval),
                "--metrics-address",
                "0.0.0.0",  # noqa: S104
                "--metrics-port",
                str(self.metrics_port),
            ]
        for path in self.static_path_mounts:
            args += ["--static-path-mount", path]
        args += ["--log-level", self.log_level, self.application_module]
        return args


class OLApplicationK8sKedaWebappScalingConfig(BaseModel):
    """KEDA-based webapp scaling configuration.

    When set on ``OLApplicationK8sConfig.webapp_keda_config``, the component
    creates a KEDA ``ScaledObject`` targeting the webapp deployment instead of a
    native ``HorizontalPodAutoscaler``.  This enables more sophisticated scaling
    triggers (Prometheus metrics, external queues) beyond what HPA natively
    supports.

    The caller is responsible for creating any ``TriggerAuthentication`` resource
    referenced by ``trigger_authentication_name``.  The component passes the name
    through to each trigger's ``authenticationRef`` automatically when provided.

    ``min_replicas`` and ``max_replicas`` are inherited from
    ``OLApplicationK8sConfig.application_min_replicas`` / ``application_max_replicas``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    polling_interval: int = Field(
        default=60, description="Seconds between trigger evaluations."
    )
    cooldown_period: int = Field(
        default=300,
        description="Seconds to wait before scaling down after last trigger.",
    )
    scale_up_stabilization_seconds: int = Field(
        default=60, description="Stabilization window before scaling up (seconds)."
    )
    scale_up_percent: int = Field(
        default=50, description="Maximum scale-up percent per period."
    )
    scale_up_period_seconds: int = Field(
        default=60, description="Period for scale-up policy evaluation (seconds)."
    )
    scale_down_stabilization_seconds: int = Field(
        default=300, description="Stabilization window before scaling down (seconds)."
    )
    scale_down_percent: int = Field(
        default=10, description="Maximum scale-down percent per period."
    )
    scale_down_period_seconds: int = Field(
        default=300, description="Period for scale-down policy evaluation (seconds)."
    )
    triggers: list[dict[str, Any]] = Field(
        description=(
            "List of KEDA trigger specifications. Each entry is a dict that matches the "
            "KEDA trigger schema (type, metadata, …). When "
            "``trigger_authentication_name`` is set, an ``authenticationRef`` block is "
            "automatically injected into any trigger that does not already define one."
        )
    )
    trigger_authentication_name: str | None = Field(
        default=None,
        description=(
            "Name of an existing KEDA ``TriggerAuthentication`` resource in the same "
            "namespace. When set, an ``authenticationRef`` block is automatically "
            "injected into every trigger that does not already define one."
        ),
    )


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
    # ConfigMap names whose key/value pairs are injected as env vars via
    # envFrom: configMapRef.  Secrets carrying sensitive values use
    # env_from_secret_names (required) above; this field is optional because
    # many callers need no flat ConfigMap env injection.
    env_from_configmap_names: list[str] = Field(default_factory=list)
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
    deployment_timeout_minutes: PositiveInt = Field(
        default=15,
        description=(
            "Minutes Pulumi will wait for the webapp Deployment and its Service to "
            "become ready before marking the update as failed. Does not affect celery "
            "worker or beat Deployments. Increase for applications with known slow "
            "rollouts. Pulumi's built-in default is 10 minutes."
        ),
    )
    init_migrations: bool = Field(default=True)
    init_collectstatic: bool = Field(default=True)
    celery_worker_configs: list[OLApplicationK8sCeleryWorkerConfig] = []
    celery_beat_config: OLApplicationK8sCeleryBeatConfig | None = None

    @model_validator(mode="after")
    def validate_beat_config(self) -> "OLApplicationK8sConfig":
        beat_workers = [w for w in self.celery_worker_configs if w.run_beat]
        if self.celery_beat_config is not None and beat_workers:
            names = ", ".join(w.worker_name or "" for w in beat_workers)
            msg = (
                f"celery_beat_config is set but worker(s) '{names}' also have "
                "run_beat=True. Use celery_beat_config exclusively."
            )
            raise ValueError(msg)
        if len(beat_workers) > 1:
            names = ", ".join(w.worker_name or "" for w in beat_workers)
            msg = (
                f"Only one worker may have run_beat=True, but found: '{names}'. "
                "Multiple beat schedulers will corrupt the RedBeat schedule in Redis."
            )
            raise ValueError(msg)
        return self

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
            failure_threshold=12,  # Allow up to 2 minutes (12 x 10s) for startup
            success_threshold=1,
            timeout_seconds=5,
        ),
    }
    app_pdb_maximum_unavailable: NonNegativeInt | str = 1
    extra_container_ports: list[kubernetes.core.v1.ContainerPortArgs] = Field(
        default_factory=list,
        description="Additional named ports to expose on the application container (e.g. a metrics port).",
    )
    granian_config: GranianConfig | None = Field(
        default=None,
        description=(
            "When set, the component generates granian cmd/args, selects the granian "
            "nginx config, and (if enable_metrics=True) exposes a metrics port and "
            "creates a PodMonitor. application_cmd_array/application_arg_array serve "
            "as the fallback command when granian_config is None."
        ),
    )
    webapp_keda_config: OLApplicationK8sKedaWebappScalingConfig | None = Field(
        default=None,
        description=(
            "When set, the component creates a KEDA ScaledObject for the webapp "
            "deployment instead of a native HorizontalPodAutoscaler. The caller is "
            "responsible for creating any TriggerAuthentication resource referenced "
            "by webapp_keda_config.trigger_authentication_name."
        ),
    )
    extra_sidecar_containers: list[kubernetes.core.v1.ContainerArgs] = Field(
        default_factory=list,
        description=(
            "Additional sidecar containers appended to the webapp pod's container list "
            "after the main application container (and after nginx, if enabled). "
            "Also applied to celery worker and beat pods."
        ),
    )
    extra_init_containers: list[kubernetes.core.v1.ContainerArgs] = Field(
        default_factory=list,
        description=(
            "Additional init containers prepended before the component-managed init "
            "containers (migrations, collectstatic) in the webapp pod. "
            "Also applied to celery worker and beat pods."
        ),
    )
    pod_security_context: kubernetes.core.v1.PodSecurityContextArgs | None = Field(
        default=None,
        description=(
            "Pod-level security context applied to all pod specs (webapp, celery "
            "workers, celery beat, and pre/post-deploy jobs). When None, no "
            "securityContext is set on pods."
        ),
    )
    extra_volumes: list[kubernetes.core.v1.VolumeArgs] = Field(
        default_factory=list,
        description=(
            "Additional volumes added to all pod specs: webapp deployment, celery "
            "worker deployments, celery beat deployment, and pre/post-deploy jobs."
        ),
    )
    extra_volume_mounts: list[kubernetes.core.v1.VolumeMountArgs] = Field(
        default_factory=list,
        description=(
            "Additional volume mounts added to the main application container, all "
            "init containers (both component-managed and extra_init_containers), "
            "pre/post-deploy job containers, and celery worker/beat containers."
        ),
    )
    extra_init_volume_mounts: list[kubernetes.core.v1.VolumeMountArgs] = Field(
        default_factory=list,
        description=(
            "Additional volume mounts added only to init containers "
            "(both component-managed and extra_init_containers). "
            "Not added to main application or celery containers."
        ),
    )
    webapp_deployment_aliases: list[Any] = Field(
        default_factory=list,
        description=(
            "Pulumi Aliases applied to the webapp Deployment. Use when migrating from "
            "hand-rolled resources to this component to prevent delete-and-recreate of "
            "the existing Deployment. Typically: [Alias(name=<old-pulumi-name>, parent=pulumi.ROOT_STACK_RESOURCE)]."
        ),
    )
    webapp_service_aliases: list[Any] = Field(
        default_factory=list,
        description=(
            "Pulumi Aliases applied to the webapp Service. Use when migrating from "
            "hand-rolled resources to this component. "
            "Typically: [Alias(name=<old-pulumi-name>, parent=pulumi.ROOT_STACK_RESOURCE)]."
        ),
    )
    webapp_keda_aliases: list[Any] = Field(
        default_factory=list,
        description=(
            "Pulumi Aliases applied to the KEDA ScaledObject for the webapp. Use when "
            "migrating from hand-rolled resources to this component. "
            "Typically: [Alias(name=<old-pulumi-name>, parent=pulumi.ROOT_STACK_RESOURCE)]."
        ),
    )

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
        return {
            key: value if isinstance(value, Output) else str(value)
            for key, value in application_config.items()
        }

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

    @model_validator(mode="after")
    def validate_granian_port_nginx_coupling(self) -> "OLApplicationK8sConfig":
        """Catch mismatched granian port / nginx config at evaluation time.

        When import_nginx_config=True the nginx sidecar proxies to a fixed upstream
        port baked into the config file. Overriding granian_config.port without also
        supplying a custom nginx_config_filename whose content matches the new port
        would silently misdirect all traffic at runtime.
        """
        gc = self.granian_config
        if (
            gc is not None
            and self.import_nginx_config
            and gc.port != DEFAULT_WSGI_PORT
            and gc.nginx_config_filename == "web.conf_granian"
        ):
            msg = (
                f"granian_config.port={gc.port} differs from DEFAULT_WSGI_PORT "
                f"({DEFAULT_WSGI_PORT}) but nginx_config_filename is still the "
                "default 'web.conf_granian'. The nginx config proxies to "
                f"127.0.0.1:{DEFAULT_WSGI_PORT}, so traffic will be misdirected. "
                "Either keep the default port or supply a custom nginx_config_filename "
                "whose upstream address matches the overridden port."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_no_duplicate_metrics_port(self) -> "OLApplicationK8sConfig":
        """Raise an error if any caller-supplied port configuration clashes with the
        auto-generated granian metrics port.

        When granian_config.enable_metrics=True the component automatically adds
        ContainerPortArgs(name='metrics', container_port=metrics_port) to the main
        application container. Three independent paths can produce a duplicate:

        1. extra_container_ports has an entry with name='metrics' or
           container_port==metrics_port — both entries land on the same container.
        2. extra_sidecar_containers has a container that declares a port with
           name='metrics' or container_port==metrics_port — the same port number
           (or name) then appears on two containers in the same pod.
        3. application_port==metrics_port — the unnamed application port and the
           named metrics port share the same number on the main container.

        In all three cases Kubernetes emits a duplicate-containerPort warning (or
        rejects the pod spec for name conflicts).
        """
        gc = self.granian_config
        if gc is None or not gc.enable_metrics:
            return self

        mport = gc.metrics_port

        # --- Gap 1: extra_container_ports (same container as metrics port) ---
        for port_arg in self.extra_container_ports:
            if getattr(port_arg, "name", None) == "metrics":
                msg = (
                    "extra_container_ports already contains a port named 'metrics'. "
                    "Remove it from extra_container_ports — the metrics port is "
                    "added automatically when granian_config.enable_metrics=True."
                )
                raise ValueError(msg)
            if getattr(port_arg, "container_port", None) == mport:
                msg = (
                    f"extra_container_ports contains a port with container_port={mport}, "
                    f"which conflicts with the auto-generated granian metrics port "
                    f"(granian_config.metrics_port={mport}). "
                    "Use a different port number or set granian_config.enable_metrics=False."
                )
                raise ValueError(msg)

        # --- Gap 2: extra_sidecar_containers (different container, same pod) ---
        for sidecar in self.extra_sidecar_containers:
            for port_arg in getattr(sidecar, "ports", None) or []:
                if getattr(port_arg, "name", None) == "metrics":
                    msg = (
                        f"Sidecar container '{getattr(sidecar, 'name', '<unknown>')}' "
                        "declares a port named 'metrics', which duplicates the "
                        "auto-generated granian metrics port name in the same pod. "
                        "Rename the sidecar port or set granian_config.enable_metrics=False."
                    )
                    raise ValueError(msg)
                if getattr(port_arg, "container_port", None) == mport:
                    msg = (
                        f"Sidecar container '{getattr(sidecar, 'name', '<unknown>')}' "
                        f"declares container_port={mport}, which duplicates the "
                        f"auto-generated granian metrics port (granian_config.metrics_port={mport}) "
                        "across containers in the same pod. "
                        "Use a different port number or set granian_config.enable_metrics=False."
                    )
                    raise ValueError(msg)

        # --- Gap 3: application_port==metrics_port (same container) ---
        if self.application_port is not None and self.application_port == mport:
            msg = (
                f"application_port={self.application_port} is the same as "
                f"granian_config.metrics_port={mport}. "
                "The component would declare two containerPorts with the same number "
                "on the main application container. "
                "Change application_port or granian_config.metrics_port so they differ."
            )
            raise ValueError(msg)

        return self


stack_info = parse_stack()


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
        # Use application_name as the Pulumi resource name so two components in the
        # same namespace (e.g. LMS + CMS) get distinct URNs.  Alias the old
        # application_namespace name so existing stacks don't see replacement.
        _component_aliases: list[Any] = (
            [Alias(name=ol_app_k8s_config.application_namespace)]
            if ol_app_k8s_config.application_namespace
            != ol_app_k8s_config.application_name
            else []
        )
        super().__init__(
            "ol:infrastructure:components:services:OLApplicationK8s",
            ol_app_k8s_config.application_name,
            None,
            opts=ResourceOptions.merge(
                opts, ResourceOptions(aliases=_component_aliases)
            ),
        )
        resource_options = ResourceOptions(parent=self)
        _deployment_timeout = CustomTimeouts(
            create=f"{ol_app_k8s_config.deployment_timeout_minutes}m",
            update=f"{ol_app_k8s_config.deployment_timeout_minutes}m",
        )
        deployment_options = ResourceOptions(
            parent=self,
            aliases=ol_app_k8s_config.webapp_deployment_aliases or None,
            custom_timeouts=_deployment_timeout,
        )

        extra_deployment_args: dict[str, int] = {}
        # KEDA ScaledObject manages replicas when webapp_keda_config is set.
        # When using native HPA with metrics, replicas are also omitted.
        # Only set a fixed replica count when neither HPA nor KEDA is active.
        if (
            ol_app_k8s_config.webapp_keda_config is not None
            or ol_app_k8s_config.hpa_scaling_metrics
        ):
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
            ),
            *ol_app_k8s_config.extra_volumes,
        ]
        nginx_volume_mounts = [
            kubernetes.core.v1.VolumeMountArgs(
                name="staticfiles",
                mount_path="/src/staticfiles",
            )
        ]
        webapp_volume_mounts = nginx_volume_mounts.copy()
        # Apply extra volume mounts to the main application container
        webapp_volume_mounts.extend(ol_app_k8s_config.extra_volume_mounts)

        app_containers = []

        # Resolve granian vs. fallback application server configuration.
        # When granian_config is set the component builds cmd/args; otherwise
        # application_cmd_array / application_arg_array are used as-is.
        effective_extra_ports = list(ol_app_k8s_config.extra_container_ports)
        if ol_app_k8s_config.granian_config is not None:
            gc = ol_app_k8s_config.granian_config
            # Derive workers_max_rss from the container memory limit when not explicit.
            # Formula: floor(memory_limit_bytes / workers * 0.9) MiB
            if (
                gc.limit_workers_max_rss
                and gc.workers_max_rss is None
                and (memory_str := ol_app_k8s_config.resource_limits.get("memory"))
            ):
                limit_bytes = int(parse_quantity(memory_str))
                computed_rss = max(
                    1, int(limit_bytes / gc.workers * 0.9) // (1024 * 1024)
                )
                gc = gc.model_copy(update={"workers_max_rss": computed_rss})
            effective_nginx_config_path = f"files/{gc.nginx_config_filename}"
            effective_cmd_array: list[str] | None = ["granian"]
            effective_arg_array: list[str] | None = gc.build_args()
            if gc.enable_metrics:
                effective_extra_ports.append(
                    kubernetes.core.v1.ContainerPortArgs(
                        name="metrics",
                        container_port=gc.metrics_port,
                    )
                )
        else:
            effective_nginx_config_path = ol_app_k8s_config.import_nginx_config_path
            effective_cmd_array = ol_app_k8s_config.application_cmd_array
            effective_arg_array = ol_app_k8s_config.application_arg_array

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
                        effective_nginx_config_path
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
        # Build a list of env sources for the deployment config via envFrom
        application_deployment_envfrom = []
        for secret_name in ol_app_k8s_config.env_from_secret_names:
            application_deployment_envfrom.append(  # noqa: PERF401
                kubernetes.core.v1.EnvFromSourceArgs(
                    secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                        name=secret_name,
                    ),
                )
            )
        for configmap_name in ol_app_k8s_config.env_from_configmap_names:
            application_deployment_envfrom.append(  # noqa: PERF401
                kubernetes.core.v1.EnvFromSourceArgs(
                    config_map_ref=kubernetes.core.v1.ConfigMapEnvSourceArgs(
                        name=configmap_name,
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
        # extra_init_containers run first, before component-managed ones
        for extra_init in ol_app_k8s_config.extra_init_containers:
            # Inject extra_volume_mounts and extra_init_volume_mounts onto each extra init container
            existing_mounts = list(getattr(extra_init, "volume_mounts", None) or [])
            extra_init = kubernetes.core.v1.ContainerArgs(  # noqa: PLW2901
                **{
                    k: v
                    for k, v in vars(extra_init).items()
                    if k != "volume_mounts" and v is not None
                },
                volume_mounts=[
                    *existing_mounts,
                    *ol_app_k8s_config.extra_volume_mounts,
                    *ol_app_k8s_config.extra_init_volume_mounts,
                ],
            )
            init_containers.append(extra_init)

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
                    volume_mounts=[
                        *ol_app_k8s_config.extra_volume_mounts,
                        *ol_app_k8s_config.extra_init_volume_mounts,
                    ],
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
                        *ol_app_k8s_config.extra_volume_mounts,
                        *ol_app_k8s_config.extra_init_volume_mounts,
                    ],
                )
            )

        # Create a deployment resource to manage the application pods
        application_labels = ol_app_k8s_config.k8s_global_labels | {
            "ol.mit.edu/component": "webapp",
            # Legacy label maintained to avoid downtime during transitions for Services
            # that still select pods using `ol.mit.edu/process`.
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

        pod_spec_args: dict[str, Any] = {}
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
        if ol_app_k8s_config.pod_security_context is not None:
            pod_spec_args["security_context"] = ol_app_k8s_config.pod_security_context

        # Shared pod spec kwargs for worker/beat pods (no anti-affinity)
        worker_pod_spec_args: dict[str, Any] = {}
        if ol_app_k8s_config.pod_security_context is not None:
            worker_pod_spec_args["security_context"] = (
                ol_app_k8s_config.pod_security_context
            )

        _application_deployment_name = truncate_k8s_metanames(
            f"{ol_app_k8s_config.application_name}-app"
        )

        # Expose deployment names as public attributes so callers can reference them
        # when configuring OLVaultK8SDynamicSecretConfig restart_targets.
        self.webapp_deployment_name: str = _application_deployment_name
        self.celery_deployment_names: list[str] = []
        self.beat_deployment_name: str | None = None

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
                            volumes=ol_app_k8s_config.extra_volumes or None,
                            init_containers=[
                                *[
                                    kubernetes.core.v1.ContainerArgs(
                                        **{
                                            k: v
                                            for k, v in vars(c).items()
                                            if k != "volume_mounts" and v is not None
                                        },
                                        volume_mounts=[
                                            *(getattr(c, "volume_mounts", None) or []),
                                            *ol_app_k8s_config.extra_volume_mounts,
                                            *ol_app_k8s_config.extra_init_volume_mounts,
                                        ],
                                    )
                                    for c in ol_app_k8s_config.extra_init_containers
                                ]
                            ]
                            or None,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name=command_name,
                                    image=app_image,
                                    command=command_array,
                                    image_pull_policy=image_pull_policy,
                                    env=application_deployment_env_vars,
                                    env_from=application_deployment_envfrom,
                                    volume_mounts=ol_app_k8s_config.extra_volume_mounts
                                    or None,
                                )
                                for (command_name, command_array) in pre_deploy_commands
                            ],
                            restart_policy="Never",
                            **worker_pod_spec_args,
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
                    ),
                    *effective_extra_ports,
                ],
                image_pull_policy=image_pull_policy,
                resources=kubernetes.core.v1.ResourceRequirementsArgs(
                    requests=ol_app_k8s_config.resource_requests,
                    limits=ol_app_k8s_config.resource_limits,
                ),
                command=effective_cmd_array,
                args=effective_arg_array,
                env=application_deployment_env_vars,
                env_from=application_deployment_envfrom,
                volume_mounts=webapp_volume_mounts,
                **ol_app_k8s_config.probe_configs,
            ),
        )
        # Append caller-supplied sidecar containers after the main app container
        app_containers.extend(ol_app_k8s_config.extra_sidecar_containers)

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
                            *init_containers,
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

        if (
            ol_app_k8s_config.granian_config is not None
            and ol_app_k8s_config.granian_config.enable_metrics
        ):
            gc = ol_app_k8s_config.granian_config
            _pod_monitor_name = truncate_k8s_metanames(
                f"{ol_app_k8s_config.application_name}-webapp-pod-monitor"
            )
            kubernetes.apiextensions.CustomResource(
                _pod_monitor_name,
                api_version="monitoring.coreos.com/v1",
                kind="PodMonitor",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=_pod_monitor_name,
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=ol_app_k8s_config.k8s_global_labels,
                ),
                spec={
                    "selector": {"matchLabels": application_labels},
                    "podMetricsEndpoints": [
                        {
                            "port": "metrics",
                            "path": "/metrics",
                            "scheme": "http",
                            "interval": f"{gc.metrics_scrape_interval}s",
                        }
                    ],
                    "namespaceSelector": {
                        "matchNames": [ol_app_k8s_config.application_namespace]
                    },
                },
                opts=resource_options,
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
                            volumes=ol_app_k8s_config.extra_volumes or None,
                            init_containers=[
                                *[
                                    kubernetes.core.v1.ContainerArgs(
                                        **{
                                            k: v
                                            for k, v in vars(c).items()
                                            if k != "volume_mounts" and v is not None
                                        },
                                        volume_mounts=[
                                            *(getattr(c, "volume_mounts", None) or []),
                                            *ol_app_k8s_config.extra_volume_mounts,
                                            *ol_app_k8s_config.extra_init_volume_mounts,
                                        ],
                                    )
                                    for c in ol_app_k8s_config.extra_init_containers
                                ]
                            ]
                            or None,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name=command_name,
                                    image=app_image,
                                    command=command_array,
                                    image_pull_policy=image_pull_policy,
                                    env=application_deployment_env_vars,
                                    env_from=application_deployment_envfrom,
                                    volume_mounts=ol_app_k8s_config.extra_volume_mounts
                                    or None,
                                )
                                for (
                                    command_name,
                                    command_array,
                                ) in post_deploy_commands
                            ],
                            restart_policy="Never",
                            **worker_pod_spec_args,
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

        if ol_app_k8s_config.webapp_keda_config is not None:
            keda_cfg = ol_app_k8s_config.webapp_keda_config

            # Build triggers: inject authenticationRef into any trigger that doesn't already have one
            def _build_keda_triggers(
                triggers: list[dict[str, Any]],
                auth_name: str | None,
            ) -> list[dict[str, Any]]:
                if not auth_name:
                    return triggers
                result = []
                for trigger in triggers:
                    t = dict(trigger)
                    if "authenticationRef" not in t:
                        t["authenticationRef"] = {"name": auth_name}
                    result.append(t)
                return result

            _webapp_scaled_object_name = truncate_k8s_metanames(
                f"{ol_app_k8s_config.application_name}-webapp-scaledobject"
            )
            kubernetes.apiextensions.CustomResource(
                f"{ol_app_k8s_config.application_name}-{stack_info.env_suffix}-webapp-scaledobject",
                api_version="keda.sh/v1alpha1",
                kind="ScaledObject",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=_webapp_scaled_object_name,
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=application_labels,
                ),
                spec={
                    "scaleTargetRef": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": _application_deployment_name,
                    },
                    "minReplicaCount": ol_app_k8s_config.application_min_replicas,
                    "maxReplicaCount": ol_app_k8s_config.application_max_replicas,
                    "pollingInterval": keda_cfg.polling_interval,
                    "cooldownPeriod": keda_cfg.cooldown_period,
                    "advanced": {
                        "horizontalPodAutoscalerConfig": {
                            "behavior": {
                                "scaleUp": {
                                    "stabilizationWindowSeconds": keda_cfg.scale_up_stabilization_seconds,
                                    "policies": [
                                        {
                                            "type": "Percent",
                                            "value": keda_cfg.scale_up_percent,
                                            "periodSeconds": keda_cfg.scale_up_period_seconds,
                                        }
                                    ],
                                },
                                "scaleDown": {
                                    "stabilizationWindowSeconds": keda_cfg.scale_down_stabilization_seconds,
                                    "policies": [
                                        {
                                            "type": "Percent",
                                            "value": keda_cfg.scale_down_percent,
                                            "periodSeconds": keda_cfg.scale_down_period_seconds,
                                        }
                                    ],
                                },
                            }
                        }
                    },
                    "triggers": _build_keda_triggers(
                        keda_cfg.triggers, keda_cfg.trigger_authentication_name
                    ),
                },
                opts=resource_options.merge(
                    ResourceOptions(
                        depends_on=[_application_deployment],
                        delete_before_replace=True,
                        aliases=ol_app_k8s_config.webapp_keda_aliases or None,
                    )
                ),
            )
        else:
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
                    min_replicas=ol_app_k8s_config.application_min_replicas,
                    max_replicas=ol_app_k8s_config.application_max_replicas,
                    metrics=ol_app_k8s_config.hpa_scaling_metrics,
                    behavior=kubernetes.autoscaling.v2.HorizontalPodAutoscalerBehaviorArgs(
                        scale_up=kubernetes.autoscaling.v2.HPAScalingRulesArgs(
                            stabilization_window_seconds=60,
                            select_policy="Max",
                            policies=[
                                kubernetes.autoscaling.v2.HPAScalingPolicyArgs(
                                    type="Percent",
                                    value=100,
                                    period_seconds=60,
                                )
                            ],
                        ),
                        scale_down=kubernetes.autoscaling.v2.HPAScalingRulesArgs(
                            stabilization_window_seconds=300,
                            select_policy="Min",
                            policies=[
                                kubernetes.autoscaling.v2.HPAScalingPolicyArgs(
                                    type="Percent",
                                    value=25,
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
            opts=resource_options.merge(
                ResourceOptions(
                    depends_on=[_application_deployment],
                    aliases=ol_app_k8s_config.webapp_service_aliases or None,
                    custom_timeouts=_deployment_timeout,
                )
            ),
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
                "ol.mit.edu/worker-name": celery_worker_config.worker_name,
            }

            # Add Slack channel label if specified
            if ol_app_k8s_config.slack_channel:
                celery_labels["ol.mit.edu/slack-channel"] = (
                    ol_app_k8s_config.slack_channel
                )

            _celery_deployment_name = truncate_k8s_metanames(
                f"{ol_app_k8s_config.application_name}-{celery_worker_config.worker_name}-celery-worker".replace(
                    "_", "-"
                )
            )
            self.celery_deployment_names.append(_celery_deployment_name)
            _celery_deployment = kubernetes.apps.v1.Deployment(
                f"{ol_app_k8s_config.application_name}-celery-worker-{celery_worker_config.worker_name}-{stack_info.env_suffix}",
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
                            volumes=ol_app_k8s_config.extra_volumes or None,
                            init_containers=[
                                *[
                                    kubernetes.core.v1.ContainerArgs(
                                        **{
                                            k: v
                                            for k, v in vars(c).items()
                                            if k != "volume_mounts" and v is not None
                                        },
                                        volume_mounts=[
                                            *(getattr(c, "volume_mounts", None) or []),
                                            *ol_app_k8s_config.extra_volume_mounts,
                                            *ol_app_k8s_config.extra_init_volume_mounts,
                                        ],
                                    )
                                    for c in ol_app_k8s_config.extra_init_containers
                                ]
                            ]
                            or None,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="celery-worker",
                                    image=app_image,
                                    command=[
                                        "celery",
                                        "-A",  # Application name
                                        celery_worker_config.application_name,
                                        "worker",  # COMMAND
                                        "-E",  # send task-related events for monitoring
                                        *(
                                            [
                                                "-Q",  # queue name filter
                                                celery_worker_config.queue_name,
                                            ]
                                            if celery_worker_config.queue_name
                                            else []
                                        ),
                                        *(
                                            [
                                                "-B",  # beat scheduler - only on one worker to avoid multiple competing schedulers
                                                "--scheduler",
                                                "redbeat.RedBeatScheduler",
                                            ]
                                            if celery_worker_config.run_beat
                                            else []
                                        ),
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
                                    volume_mounts=ol_app_k8s_config.extra_volume_mounts
                                    or None,
                                ),
                                *ol_app_k8s_config.extra_sidecar_containers,
                            ],
                            **worker_pod_spec_args,
                        ),
                    ),
                ),
                opts=resource_options,
            )

            _celery_scaled_object = kubernetes.apiextensions.CustomResource(
                f"{ol_app_k8s_config.application_name}-celery-worker-{celery_worker_config.worker_name}-{stack_info.env_suffix}-scaledobject",
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
                                    ].worker_name,
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

        if ol_app_k8s_config.celery_beat_config is not None:
            beat_config = ol_app_k8s_config.celery_beat_config
            beat_labels = ol_app_k8s_config.k8s_global_labels | {
                "ol.mit.edu/component": str(Component.celery),
                "ol.mit.edu/application": f"{ol_app_k8s_config.application_name}",
                "ol.mit.edu/pod-security-group": ol_app_k8s_config.application_security_group_name.apply(
                    truncate_k8s_metanames
                ),
                "ol.mit.edu/worker-name": "beat",
            }
            if ol_app_k8s_config.slack_channel:
                beat_labels["ol.mit.edu/slack-channel"] = (
                    ol_app_k8s_config.slack_channel
                )
            _beat_deployment_name = truncate_k8s_metanames(
                f"{ol_app_k8s_config.application_name}-celery-beat".replace("_", "-")
            )
            self.beat_deployment_name = _beat_deployment_name
            _beat_deployment = kubernetes.apps.v1.Deployment(
                f"{ol_app_k8s_config.application_name}-celery-beat-{stack_info.env_suffix}",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=_beat_deployment_name,
                    namespace=ol_app_k8s_config.application_namespace,
                    labels=beat_labels,
                ),
                spec=kubernetes.apps.v1.DeploymentSpecArgs(
                    replicas=1,
                    selector=kubernetes.meta.v1.LabelSelectorArgs(
                        match_labels=beat_labels,
                    ),
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels=beat_labels,
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            service_account_name=ol_app_k8s_config.application_service_account_name,
                            dns_policy="ClusterFirst",
                            volumes=ol_app_k8s_config.extra_volumes or None,
                            init_containers=[
                                *[
                                    kubernetes.core.v1.ContainerArgs(
                                        **{
                                            k: v
                                            for k, v in vars(c).items()
                                            if k != "volume_mounts" and v is not None
                                        },
                                        volume_mounts=[
                                            *(getattr(c, "volume_mounts", None) or []),
                                            *ol_app_k8s_config.extra_volume_mounts,
                                            *ol_app_k8s_config.extra_init_volume_mounts,
                                        ],
                                    )
                                    for c in ol_app_k8s_config.extra_init_containers
                                ]
                            ]
                            or None,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="celery-beat",
                                    image=app_image,
                                    command=[
                                        "celery",
                                        "-A",
                                        beat_config.application_name,
                                        "beat",
                                        "--scheduler",
                                        beat_config.scheduler,
                                        "-l",
                                        beat_config.log_level,
                                    ],
                                    env=application_deployment_env_vars,
                                    env_from=application_deployment_envfrom,
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests=beat_config.resource_requests,
                                        limits=beat_config.resource_limits,
                                    ),
                                    volume_mounts=ol_app_k8s_config.extra_volume_mounts
                                    or None,
                                ),
                                *ol_app_k8s_config.extra_sidecar_containers,
                            ],
                            **worker_pod_spec_args,
                        ),
                    ),
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

    @property
    def all_deployment_names(self) -> list[str]:
        """All Kubernetes Deployment names managed by this component.

        Includes the webapp deployment, all celery worker deployments, and the
        celery beat deployment (if configured).  Use this to populate
        ``restart_targets`` on ``OLVaultK8SDynamicSecretConfig`` so that all
        pods restart when Vault dynamic credentials are rotated:

        .. code-block:: python

            app = OLApplicationK8s(config)
            OLVaultK8SDynamicSecretConfig(
                ...
                restart_targets=[
                    OLVaultRestartTarget(kind="Deployment", name=name)
                    for name in app.all_deployment_names
                ],
            )
        """
        names = [self.webapp_deployment_name]
        names.extend(self.celery_deployment_names)
        if self.beat_deployment_name:
            names.append(self.beat_deployment_name)
        return names
