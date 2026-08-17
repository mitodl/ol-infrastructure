"""Tests for OLApplicationK8s-related configuration helpers.

This module verifies:
1. GranianConfig.static_path_mounts produces correct granian args
2. OLApplicationK8sCeleryBeatConfig.application_name is propagated correctly
3. Default values and simple overrides on OLApplicationK8s*Config data models
   behave as expected
4. validate_no_duplicate_metrics_port catches all three duplication paths:
   - extra_container_ports with name='metrics' or same port number
   - extra_sidecar_containers with name='metrics' or same port number
   - application_port == metrics_port

Note:
    Most of these tests operate at the configuration/model level and do not
    instantiate OLApplicationK8s or assert on full Kubernetes pod specs (e.g.,
    sidecars, init containers, volumes, or pod_security_context), nor do they
    assert on autoscaling resources such as HPAs or KEDA ScaledObjects. The
    exceptions are test_default_container_annotation_set_to_app_container, which
    instantiates OLApplicationK8s under Pulumi mocks to verify the Deployment's
    pod template annotations, and the container_security_context tests at the
    end of the module, which assert on the rendered container specs.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())

import pulumi_kubernetes as kubernetes  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from bridge.lib.magic_numbers import (  # noqa: E402
    DEFAULT_NGINX_PORT,
    DEFAULT_WSGI_PORT,
)
from ol_infrastructure.components.services.k8s import (  # noqa: E402
    GranianConfig,
    OLApplicationK8s,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
    OLApplicationK8sKedaWebappScalingConfig,
    default_probe_configs,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _base_config(**overrides) -> OLApplicationK8sConfig:
    """Return a minimal OLApplicationK8sConfig suitable for testing."""
    defaults = {
        "application_name": "myapp",
        "application_namespace": "myapp-ns",
        "application_image_repository": "registry.example.com/myapp",
        "application_docker_tag": "latest",
        "application_security_group_id": pulumi.Output.from_input("sg-test"),
        "application_security_group_name": pulumi.Output.from_input("myapp-sg"),
        "application_service_account_name": "myapp-sa",
        "application_lb_service_name": "myapp-service",
        "application_lb_service_port_name": "http",
        "application_config": {},
        "env_from_secret_names": ["myapp-secret"],
        "vault_k8s_resource_auth_name": "myapp-vault-auth",
        "project_root": "/tmp/myapp",  # noqa: S108
        "import_nginx_config": False,
        "k8s_global_labels": {
            "ol.mit.edu/application": "myapp",
            "ol.mit.edu/environment": "qa",
        },
    }
    defaults.update(overrides)
    return OLApplicationK8sConfig(**defaults)


# ─── GranianConfig.static_path_mounts ─────────────────────────────────────────


def test_granian_config_no_static_path_mounts():
    gc = GranianConfig(application_module="myapp.wsgi:application")
    args = gc.build_args()
    assert "--static-path-mount" not in args


def test_granian_config_single_static_path_mount():
    gc = GranianConfig(
        application_module="myapp.wsgi:application",
        static_path_mounts=["/staticfiles"],
    )
    args = gc.build_args()
    idx = args.index("--static-path-mount")
    assert args[idx + 1] == "/staticfiles"
    assert args.count("--static-path-mount") == 1


def test_granian_config_multiple_static_path_mounts():
    gc = GranianConfig(
        application_module="myapp.wsgi:application",
        static_path_mounts=["/static", "/media"],
    )
    args = gc.build_args()
    assert args.count("--static-path-mount") == 2
    indices = [i for i, a in enumerate(args) if a == "--static-path-mount"]
    values = [args[i + 1] for i in indices]
    assert "/static" in values
    assert "/media" in values


def test_granian_config_static_path_mounts_before_log_level():
    gc = GranianConfig(
        application_module="myapp.wsgi:application",
        static_path_mounts=["/static"],
    )
    args = gc.build_args()
    mount_idx = args.index("--static-path-mount")
    log_idx = args.index("--log-level")
    assert mount_idx < log_idx


# ─── GranianConfig.static_path_expires ────────────────────────────────────────


def test_granian_config_no_static_path_expires():
    """Omitted by default, so Granian keeps its own 86400 default."""
    gc = GranianConfig(application_module="myapp.wsgi:application")
    assert "--static-path-expires" not in gc.build_args()


def test_granian_config_static_path_expires_emitted():
    gc = GranianConfig(
        application_module="myapp.wsgi:application",
        static_path_mounts=["/staticfiles"],
        static_path_expires=315360000,
    )
    args = gc.build_args()
    idx = args.index("--static-path-expires")
    assert args[idx + 1] == "315360000"


def test_granian_config_static_path_expires_accepts_zero():
    """Zero is meaningful upstream -- it disables the Cache-Control header."""
    gc = GranianConfig(
        application_module="myapp.wsgi:application",
        static_path_expires=0,
    )
    args = gc.build_args()
    idx = args.index("--static-path-expires")
    assert args[idx + 1] == "0"


def test_granian_config_static_path_expires_rejects_negative():
    with pytest.raises(ValidationError):
        GranianConfig(
            application_module="myapp.wsgi:application",
            static_path_expires=-1,
        )


def test_granian_config_static_path_expires_before_log_level():
    gc = GranianConfig(
        application_module="myapp.wsgi:application",
        static_path_expires=60,
    )
    args = gc.build_args()
    assert args.index("--static-path-expires") < args.index("--log-level")


# ─── OLApplicationK8sCeleryBeatConfig.application_name ────────────────────────


def test_celery_beat_config_default_application_name():
    config = OLApplicationK8sCeleryBeatConfig()
    assert config.application_name == "main.celery:app"


def test_celery_beat_config_custom_application_name():
    config = OLApplicationK8sCeleryBeatConfig(application_name="lms.celery:app")
    assert config.application_name == "lms.celery:app"


# ─── OLApplicationK8sKedaWebappScalingConfig ──────────────────────────────────


def test_keda_webapp_config_defaults():
    cfg = OLApplicationK8sKedaWebappScalingConfig(
        triggers=[{"type": "prometheus", "metadata": {"query": "up"}}]
    )
    assert cfg.scale_up_stabilization_seconds == 60
    assert cfg.scale_down_stabilization_seconds == 300
    assert cfg.polling_interval == 60
    assert cfg.cooldown_period == 300
    assert cfg.trigger_authentication_name is None


def test_keda_webapp_config_with_auth_ref():
    cfg = OLApplicationK8sKedaWebappScalingConfig(
        triggers=[{"type": "prometheus", "metadata": {"query": "up"}}],
        trigger_authentication_name="grafana-cloud-auth",
    )
    assert cfg.trigger_authentication_name == "grafana-cloud-auth"


def test_keda_webapp_config_custom_behavior():
    cfg = OLApplicationK8sKedaWebappScalingConfig(
        triggers=[{"type": "cpu", "metadata": {"type": "AverageValue", "value": "4"}}],
        scale_up_stabilization_seconds=30,
        scale_down_stabilization_seconds=600,
        scale_down_percent=5,
        polling_interval=30,
        cooldown_period=120,
    )
    assert cfg.scale_up_stabilization_seconds == 30
    assert cfg.scale_down_stabilization_seconds == 600
    assert cfg.scale_down_percent == 5
    assert cfg.polling_interval == 30
    assert cfg.cooldown_period == 120


# ─── OLApplicationK8sConfig model fields ──────────────────────────────────────


def test_app_config_webapp_keda_config_field():
    cfg = _base_config(
        webapp_keda_config=OLApplicationK8sKedaWebappScalingConfig(
            triggers=[
                {"type": "cpu", "metadata": {"type": "AverageValue", "value": "4"}}
            ]
        )
    )
    assert cfg.webapp_keda_config is not None


def test_app_config_extra_sidecar_containers_default_empty():
    cfg = _base_config()
    assert cfg.extra_sidecar_containers == []


def test_app_config_extra_init_containers_default_empty():
    cfg = _base_config()
    assert cfg.extra_init_containers == []


def test_app_config_pod_security_context_default_none():
    cfg = _base_config()
    assert cfg.pod_security_context is None


def test_app_config_extra_volumes_default_empty():
    cfg = _base_config()
    assert cfg.extra_volumes == []


def test_app_config_extra_volume_mounts_default_empty():
    cfg = _base_config()
    assert cfg.extra_volume_mounts == []


def test_app_config_extra_init_volume_mounts_default_empty():
    cfg = _base_config()
    assert cfg.extra_init_volume_mounts == []


def test_app_config_pod_security_context_accepts_args():
    ctx = kubernetes.core.v1.PodSecurityContextArgs(
        run_as_user=1000,
        run_as_group=1000,
        fs_group=1000,
    )
    cfg = _base_config(pod_security_context=ctx)
    assert cfg.pod_security_context is ctx


def test_app_config_extra_volumes_accepts_volume_list():
    vol = kubernetes.core.v1.VolumeArgs(
        name="my-config",
        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(name="my-configmap"),
    )
    cfg = _base_config(extra_volumes=[vol])
    assert len(cfg.extra_volumes) == 1


def test_app_config_extra_sidecar_accepts_container_list():
    sidecar = kubernetes.core.v1.ContainerArgs(
        name="vector",
        image="timberio/vector:latest-alpine",
    )
    cfg = _base_config(extra_sidecar_containers=[sidecar])
    assert len(cfg.extra_sidecar_containers) == 1


def test_app_config_extra_init_containers_accepts_container_list():
    init = kubernetes.core.v1.ContainerArgs(
        name="mkdir",
        image="busybox",
        command=["mkdir", "-p", "/data/exports"],
    )
    cfg = _base_config(extra_init_containers=[init])
    assert len(cfg.extra_init_containers) == 1


def test_app_config_extra_volume_mounts_accepts_mount_list():
    mount = kubernetes.core.v1.VolumeMountArgs(
        name="my-config",
        mount_path="/config",
    )
    cfg = _base_config(extra_volume_mounts=[mount])
    assert len(cfg.extra_volume_mounts) == 1


def test_app_config_extra_init_volume_mounts_accepts_mount_list():
    mount = kubernetes.core.v1.VolumeMountArgs(
        name="edxapp-config",
        mount_path="/edx/etc",
    )
    cfg = _base_config(extra_init_volume_mounts=[mount])
    assert len(cfg.extra_init_volume_mounts) == 1


# ─── OLApplicationK8sCeleryWorkerConfig.application_name ─────────────────────


def test_celery_worker_config_default_application_name():
    cfg = OLApplicationK8sCeleryWorkerConfig(
        queue_name="default",
        redis_host=pulumi.Output.from_input("redis.example.com"),
        redis_password="secret",  # pragma: allowlist secret
    )
    assert cfg.application_name == "main.celery:app"


def test_celery_worker_config_custom_application_name():
    cfg = OLApplicationK8sCeleryWorkerConfig(
        queue_name="default",
        application_name="lms.celery:app",
        redis_host=pulumi.Output.from_input("redis.example.com"),
        redis_password="secret",  # pragma: allowlist secret
    )
    assert cfg.application_name == "lms.celery:app"


# ─── Composed scenario: edxapp-like config ────────────────────────────────────


def test_edxapp_like_config_composes_cleanly():
    """Simulate a realistic edxapp config to catch any Pydantic validation errors."""
    cfg = _base_config(
        application_name="lms",
        import_nginx_config=False,
        granian_config=GranianConfig(
            application_module="lms.wsgi:application",
            static_path_mounts=["/openedx/staticfiles"],
            port=8000,
        ),
        pod_security_context=kubernetes.core.v1.PodSecurityContextArgs(
            run_as_user=1000,
            run_as_group=1000,
            fs_group=1000,
        ),
        extra_volumes=[
            kubernetes.core.v1.VolumeArgs(
                name="edxapp-config",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
        ],
        extra_volume_mounts=[
            kubernetes.core.v1.VolumeMountArgs(
                name="edxapp-config",
                mount_path="/edx/etc",
            ),
        ],
        extra_init_containers=[
            kubernetes.core.v1.ContainerArgs(
                name="config-aggregator",
                image="busybox",
                command=["sh", "-c", "cat /secrets/* > /edx/etc/lms.env.yml"],
            ),
        ],
        extra_sidecar_containers=[
            kubernetes.core.v1.ContainerArgs(
                name="vector",
                image="timberio/vector:latest-alpine",
            ),
        ],
        webapp_keda_config=OLApplicationK8sKedaWebappScalingConfig(
            triggers=[
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": "https://prometheus.example.com",
                        "query": "sum(rate(django_http_requests_total[1m]))",
                        "threshold": "100",
                    },
                }
            ],
            trigger_authentication_name="grafana-cloud-triggerauth",
        ),
        celery_beat_config=OLApplicationK8sCeleryBeatConfig(
            application_name="lms.celery:app",
        ),
    )

    # Verify all new fields are correctly set
    assert cfg.pod_security_context is not None
    assert len(cfg.extra_volumes) == 1
    assert len(cfg.extra_volume_mounts) == 1
    assert len(cfg.extra_init_containers) == 1
    assert len(cfg.extra_sidecar_containers) == 1
    assert cfg.webapp_keda_config is not None
    assert (
        cfg.webapp_keda_config.trigger_authentication_name
        == "grafana-cloud-triggerauth"
    )
    assert cfg.celery_beat_config is not None
    assert cfg.celery_beat_config.application_name == "lms.celery:app"
    assert cfg.granian_config is not None
    assert "/openedx/staticfiles" in cfg.granian_config.static_path_mounts


# ─── celery beat application_name integration ──────────────────────────────────


def test_celery_beat_uses_custom_application_name():
    """Verify beat config application_name replaces the hardcoded default."""
    beat_cfg = OLApplicationK8sCeleryBeatConfig(application_name="lms.celery:app")
    # Just verify the config is constructed properly; the actual deployment
    # creation would require a full stack setup. The data model test above
    # is sufficient for unit testing this path.
    assert beat_cfg.application_name == "lms.celery:app"
    assert beat_cfg.application_name != "main.celery:app"


# ─── Deployment pod template annotations ──────────────────────────────────────


@pulumi.runtime.test
def test_default_container_annotation_set_to_app_container():
    """Deployment pod template must point kubectl exec/logs at the app container."""
    cfg = _base_config(application_name="myapp")
    app = OLApplicationK8s(cfg)

    def check(annotations):
        assert annotations["kubectl.kubernetes.io/default-container"] == "myapp-app"

    return app.application_deployment.spec.template.metadata.annotations.apply(check)


@pulumi.runtime.test
def test_config_hash_annotation_absent_without_sources():
    """No config-hash annotation when there's nothing to hash."""
    cfg = _base_config(application_name="myapp")
    app = OLApplicationK8s(cfg)

    def check(annotations):
        assert "ol.mit.edu/config-hash" not in annotations

    return app.application_deployment.spec.template.metadata.annotations.apply(check)


@pulumi.runtime.test
def test_config_hash_annotation_present_with_config_hash_inputs():
    """config_hash_inputs must produce a config-hash annotation."""
    cfg = _base_config(
        application_name="myapp", config_hash_inputs={"secret-version": "1"}
    )
    app = OLApplicationK8s(cfg)

    def check(annotations):
        assert "ol.mit.edu/config-hash" in annotations
        assert len(annotations["ol.mit.edu/config-hash"]) == 64  # sha256 hex digest

    return app.application_deployment.spec.template.metadata.annotations.apply(check)


@pulumi.runtime.test
def test_config_hash_annotation_changes_with_input():
    """Changing a config_hash_inputs value must change the resulting hash."""
    app_a = OLApplicationK8s(
        _base_config(
            application_name="myapp-a", config_hash_inputs={"secret-version": "1"}
        )
    )
    app_b = OLApplicationK8s(
        _base_config(
            application_name="myapp-b", config_hash_inputs={"secret-version": "2"}
        )
    )

    def check(hashes):
        assert hashes[0] != hashes[1]

    return pulumi.Output.all(
        app_a.application_deployment.spec.template.metadata.annotations.apply(
            lambda a: a["ol.mit.edu/config-hash"]
        ),
        app_b.application_deployment.spec.template.metadata.annotations.apply(
            lambda a: a["ol.mit.edu/config-hash"]
        ),
    ).apply(check)


# ─── validate_no_duplicate_metrics_port ────────────────────────────────────────
# Gap 1: extra_container_ports conflicts


def test_extra_container_ports_named_metrics_raises():
    """Clashes: extra_container_ports name='metrics' vs auto-added granian port."""
    with pytest.raises(ValidationError, match="port named 'metrics'"):
        _base_config(
            granian_config=GranianConfig(application_module="app.wsgi:application"),
            extra_container_ports=[
                kubernetes.core.v1.ContainerPortArgs(
                    name="metrics", container_port=9090
                )
            ],
        )


def test_extra_container_ports_same_number_raises():
    """Clashes: extra_container_ports with same port number, different name."""
    with pytest.raises(ValidationError, match="container_port=9090"):
        _base_config(
            granian_config=GranianConfig(application_module="app.wsgi:application"),
            extra_container_ports=[
                kubernetes.core.v1.ContainerPortArgs(
                    name="prometheus", container_port=9090
                )
            ],
        )


def test_extra_container_ports_different_port_ok():
    """extra_container_ports with a different port number is fine."""
    cfg = _base_config(
        granian_config=GranianConfig(application_module="app.wsgi:application"),
        extra_container_ports=[
            kubernetes.core.v1.ContainerPortArgs(name="debug", container_port=5678)
        ],
    )
    assert len(cfg.extra_container_ports) == 1


def test_extra_container_ports_no_granian_ok():
    """extra_container_ports with any port is fine when granian_config is None."""
    cfg = _base_config(
        granian_config=None,
        extra_container_ports=[
            kubernetes.core.v1.ContainerPortArgs(name="metrics", container_port=9090)
        ],
    )
    assert len(cfg.extra_container_ports) == 1


def test_extra_container_ports_metrics_disabled_ok():
    """extra_container_ports with name='metrics' is fine when enable_metrics=False."""
    cfg = _base_config(
        granian_config=GranianConfig(
            application_module="app.wsgi:application", enable_metrics=False
        ),
        extra_container_ports=[
            kubernetes.core.v1.ContainerPortArgs(name="metrics", container_port=9090)
        ],
    )
    assert len(cfg.extra_container_ports) == 1


# Gap 2: extra_sidecar_containers conflicts


def test_sidecar_with_metrics_name_raises():
    """Clashes: sidecar port named 'metrics' duplicates the granian metrics port."""
    sidecar_with_metrics = kubernetes.core.v1.ContainerArgs(
        name="vector",
        image="timberio/vector:latest-alpine",
        ports=[
            kubernetes.core.v1.ContainerPortArgs(name="metrics", container_port=9090)
        ],
    )
    with pytest.raises(ValidationError, match="port named 'metrics'"):
        _base_config(
            granian_config=GranianConfig(application_module="app.wsgi:application"),
            extra_sidecar_containers=[sidecar_with_metrics],
        )


def test_sidecar_with_same_port_number_raises():
    """Clashes: sidecar container_port==metrics_port duplicates port across the pod."""
    sidecar_with_9090 = kubernetes.core.v1.ContainerArgs(
        name="exporter",
        image="prom/node-exporter:latest",
        ports=[
            kubernetes.core.v1.ContainerPortArgs(
                name="prom-metrics", container_port=9090
            )
        ],
    )
    with pytest.raises(ValidationError, match="container_port=9090"):
        _base_config(
            granian_config=GranianConfig(application_module="app.wsgi:application"),
            extra_sidecar_containers=[sidecar_with_9090],
        )


def test_sidecar_with_different_port_ok():
    """A sidecar with a different port number is fine."""
    sidecar = kubernetes.core.v1.ContainerArgs(
        name="vector",
        image="timberio/vector:latest-alpine",
        ports=[
            kubernetes.core.v1.ContainerPortArgs(name="vector-api", container_port=8686)
        ],
    )
    cfg = _base_config(
        granian_config=GranianConfig(application_module="app.wsgi:application"),
        extra_sidecar_containers=[sidecar],
    )
    assert len(cfg.extra_sidecar_containers) == 1


def test_sidecar_without_ports_ok():
    """The edxapp vector sidecar (no ports declared) must not trigger validation."""
    vector_sidecar = kubernetes.core.v1.ContainerArgs(
        name="vector",
        image="timberio/vector:0.34.1-alpine",
    )
    cfg = _base_config(
        granian_config=GranianConfig(
            application_module="lms.wsgi:application", port=8000
        ),
        extra_sidecar_containers=[vector_sidecar],
    )
    assert len(cfg.extra_sidecar_containers) == 1


# Gap 3: application_port == metrics_port


def test_application_port_equals_metrics_port_raises():
    """Clashes: application_port==metrics_port causes duplicate port numbers.

    Both ports land on the main container.
    """
    with pytest.raises(ValidationError, match="application_port=9090"):
        _base_config(
            granian_config=GranianConfig(
                application_module="app.wsgi:application", metrics_port=9090
            ),
            application_port=9090,
        )


def test_application_port_differs_from_metrics_port_ok():
    """application_port different from metrics_port is fine."""
    cfg = _base_config(
        granian_config=GranianConfig(
            application_module="app.wsgi:application", port=8000, metrics_port=9090
        ),
        application_port=8000,
    )
    assert cfg.application_port == 8000


def test_application_port_none_ok():
    """application_port=None never triggers the application_port==metrics_port check."""
    cfg = _base_config(
        granian_config=GranianConfig(
            application_module="app.wsgi:application", metrics_port=9090
        ),
        application_port=None,
    )
    assert cfg.application_port is None


# ─── Application port alignment ───────────────────────────────────────────────
#
# The Service port, the exported application_lb_service_port, the container
# port and the default probe ports all have to agree. They are computed in
# four different places, and when they disagree the symptom is a 502 on every
# request with nothing in the Pulumi diff to point at it -- the route resource
# does not change at all. Hence asserting on all four together.


# Under Pulumi mocks the resource inputs come back keyed by the Python argument
# names (liveness_probe, container_port) rather than the camelCase Kubernetes
# wire format, so these use snake_case throughout. Getting this wrong makes the
# "probe is absent" assertions below silently vacuous.
_PROBE_KEYS = ("liveness_probe", "readiness_probe", "startup_probe")


def _probe_ports(container) -> set[int]:
    return {container[key]["http_get"]["port"] for key in _PROBE_KEYS}


def _app_container(containers, application_name="myapp"):
    return next(c for c in containers if c["name"] == f"{application_name}-app")


def test_default_probe_configs_follow_port():
    """The generated probes all target the port they were built for."""
    probes = default_probe_configs(9999)
    assert set(probes) == {"liveness_probe", "readiness_probe", "startup_probe"}
    for probe in probes.values():
        assert probe.http_get.port == 9999


def test_default_probe_configs_paths_unchanged():
    """django-health-check endpoints, one per probe kind."""
    probes = default_probe_configs(DEFAULT_WSGI_PORT)
    assert probes["liveness_probe"].http_get.path == "/health/liveness/"
    assert probes["readiness_probe"].http_get.path == "/health/readiness/"
    assert probes["startup_probe"].http_get.path == "/health/startup/"


@pulumi.runtime.test
def test_ports_align_without_nginx_sidecar():
    """No sidecar: container, probes and Service all land on DEFAULT_WSGI_PORT."""
    app = OLApplicationK8s(_base_config(application_name="noproxy"))
    assert app.application_lb_service_port == DEFAULT_WSGI_PORT

    def check(args):
        containers, service_ports = args
        container = _app_container(containers, "noproxy")
        assert container["ports"][0]["container_port"] == DEFAULT_WSGI_PORT
        assert _probe_ports(container) == {DEFAULT_WSGI_PORT}
        assert service_ports[0]["port"] == DEFAULT_WSGI_PORT
        assert service_ports[0]["target_port"] == DEFAULT_WSGI_PORT

    return pulumi.Output.all(
        app.application_deployment.spec.template.spec.containers,
        app.application_service.spec.ports,
    ).apply(check)


@pulumi.runtime.test
def test_ports_align_with_nginx_sidecar():
    """Sidecar on: the app container and probes stay on the nginx port.

    The regression guard for the six apps that still have a sidecar -- the
    probe-config refactor must not move them off 8071.
    """
    project_root = Path(tempfile.mkdtemp())
    (project_root / "files").mkdir()
    (project_root / "files" / "web.conf").write_text("server { listen 8071; }\n")

    app = OLApplicationK8s(
        _base_config(
            application_name="sidecarapp",
            project_root=project_root,
            import_nginx_config=True,
        )
    )
    assert app.application_lb_service_port == DEFAULT_NGINX_PORT

    def check(args):
        containers, service_ports = args
        container = _app_container(containers, "sidecarapp")
        assert container["ports"][0]["container_port"] == DEFAULT_NGINX_PORT
        assert _probe_ports(container) == {DEFAULT_NGINX_PORT}
        assert service_ports[0]["port"] == DEFAULT_NGINX_PORT

    return pulumi.Output.all(
        app.application_deployment.spec.template.spec.containers,
        app.application_service.spec.ports,
    ).apply(check)


@pulumi.runtime.test
def test_ports_align_with_explicit_application_port():
    """An explicit application_port drags the Service and the probes with it."""
    app = OLApplicationK8s(
        _base_config(application_name="customport", application_port=8123)
    )
    assert app.application_lb_service_port == 8123

    def check(args):
        containers, service_ports = args
        container = _app_container(containers, "customport")
        assert container["ports"][0]["container_port"] == 8123
        assert _probe_ports(container) == {8123}
        assert service_ports[0]["port"] == 8123

    return pulumi.Output.all(
        app.application_deployment.spec.template.spec.containers,
        app.application_service.spec.ports,
    ).apply(check)


@pulumi.runtime.test
def test_explicit_probe_configs_are_not_overridden():
    """A caller that supplies probes owns the port it names."""
    app = OLApplicationK8s(
        _base_config(
            application_name="ownprobes",
            probe_configs={
                "liveness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/custom/", port=7777
                    )
                )
            },
        )
    )

    def check(containers):
        container = _app_container(containers, "ownprobes")
        assert container["liveness_probe"]["http_get"]["port"] == 7777
        assert container["liveness_probe"]["http_get"]["path"] == "/custom/"
        assert "readiness_probe" not in container
        assert "startup_probe" not in container

    return app.application_deployment.spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_empty_probe_configs_disables_probes():
    """An explicit empty mapping means no probes, not 'give me the defaults'."""
    app = OLApplicationK8s(_base_config(application_name="noprobes", probe_configs={}))

    def check(containers):
        container = _app_container(containers, "noprobes")
        # _PROBE_KEYS is the same tuple the alignment tests above index with, so
        # a wrong spelling fails there rather than making this pass vacuously.
        assert not any(key in container for key in _PROBE_KEYS)

    return app.application_deployment.spec.template.spec.containers.apply(check)


# ─── container_security_context ───────────────────────────────────────────────

_HARDENED_CONTAINER_CONTEXT = kubernetes.core.v1.SecurityContextArgs(
    allow_privilege_escalation=False,
    read_only_root_filesystem=True,
    capabilities=kubernetes.core.v1.CapabilitiesArgs(drop=["ALL"]),
)


@pulumi.runtime.test
def test_container_security_context_absent_by_default():
    """Unset means no securityContext key at all, not an empty block."""
    app = OLApplicationK8s(_base_config(application_name="nocontext"))

    def check(containers):
        assert "security_context" not in _app_container(containers, "nocontext")

    return app.application_deployment.spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_container_security_context_applied_to_app_container():
    app = OLApplicationK8s(
        _base_config(
            application_name="hardened",
            container_security_context=_HARDENED_CONTAINER_CONTEXT,
        )
    )

    def check(containers):
        context = _app_container(containers, "hardened")["security_context"]
        assert context["read_only_root_filesystem"] is True
        assert context["allow_privilege_escalation"] is False
        assert context["capabilities"]["drop"] == ["ALL"]

    return app.application_deployment.spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_container_security_context_skips_nginx_sidecar():
    """Nginx needs a writable /var/cache/nginx, so it keeps the image's own context."""
    project_root = Path(tempfile.mkdtemp())
    (project_root / "files").mkdir()
    (project_root / "files" / "web.conf").write_text("server { listen 8071; }\n")

    app = OLApplicationK8s(
        _base_config(
            application_name="withnginx",
            project_root=project_root,
            import_nginx_config=True,
            container_security_context=_HARDENED_CONTAINER_CONTEXT,
        )
    )

    def check(containers):
        nginx = next(c for c in containers if c["name"] == "nginx")
        assert "security_context" not in nginx
        assert "security_context" in _app_container(containers, "withnginx")

    return app.application_deployment.spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_container_security_context_applied_to_celery_worker():
    app = OLApplicationK8s(
        _base_config(
            application_name="hardenedcelery",
            container_security_context=_HARDENED_CONTAINER_CONTEXT,
            celery_worker_configs=[
                OLApplicationK8sCeleryWorkerConfig(
                    application_name="hardenedcelery",
                    worker_name="default",
                    redis_host=pulumi.Output.from_input("redis.example.com"),
                    redis_password="hunter2",  # pragma: allowlist secret
                )
            ],
        )
    )

    def check(containers):
        worker = next(c for c in containers if c["name"] == "celery-worker")
        assert worker["security_context"]["read_only_root_filesystem"] is True

    return app.celery_deployments[0].spec.template.spec.containers.apply(check)


# ─── Granian webapp PodMonitor selector ───────────────────────────────────────


@pulumi.runtime.test
def test_pod_monitor_selector_excludes_security_group_labels():
    """The PodMonitor must not select on either pod-security-group label.

    Prometheus SD flattens a label name by replacing every non-alphanumeric
    character with ``_``, so ``ol.mit.edu/pod-security-group`` and
    ``ol.mit.edu/pod_security_group`` collapse to the same meta-label. Selecting
    on both emits two ``keep`` rules against that one meta-label demanding
    different values, the scrape pool resolves to zero targets, and the app goes
    silently unmonitored -- no ``up`` series and no error. mitxonline lost every
    granian_* metric this way.
    """
    app = OLApplicationK8s(
        _base_config(
            application_name="monitored",
            k8s_global_labels={
                "ol.mit.edu/application": "monitored",
                "ol.mit.edu/environment": "qa",
                "ol.mit.edu/pod_security_group": "monitored",
            },
            granian_config=GranianConfig(
                application_module="monitored.wsgi:application",
                enable_metrics=True,
            ),
        )
    )

    def check(spec):
        match_labels = spec["selector"]["matchLabels"]
        assert "ol.mit.edu/pod-security-group" not in match_labels
        assert "ol.mit.edu/pod_security_group" not in match_labels
        assert match_labels == {
            "ol.mit.edu/application": "monitored",
            "ol.mit.edu/component": "webapp",
        }

    return app.webapp_pod_monitor.spec.apply(check)


# ─── celery --max-memory-per-child ────────────────────────────────────────────


def _celery_worker_config(**overrides) -> OLApplicationK8sCeleryWorkerConfig:
    defaults = {
        "application_name": "memcapped",
        "worker_name": "default",
        "redis_host": pulumi.Output.from_input("redis.example.com"),
        "redis_password": "hunter2",  # pragma: allowlist secret
    }
    defaults.update(overrides)
    return OLApplicationK8sCeleryWorkerConfig(**defaults)


@pulumi.runtime.test
def test_max_memory_per_child_omitted_by_default():
    """Existing OLApplicationK8s consumers must be unaffected.

    The flag changes when celery retires a pool child, so it has to stay opt-in
    rather than arriving with a default that silently reshapes every other
    application's worker recycling behaviour.
    """
    app = OLApplicationK8s(
        _base_config(
            application_name="memcapped",
            celery_worker_configs=[_celery_worker_config()],
        )
    )

    def check(containers):
        worker = next(c for c in containers if c["name"] == "celery-worker")
        assert "--max-memory-per-child" not in worker["command"]
        # the sibling recycle trigger stays unconditional
        assert "--max-tasks-per-child" in worker["command"]

    return app.celery_deployments[0].spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_max_memory_per_child_emitted_as_flag_and_value():
    app = OLApplicationK8s(
        _base_config(
            application_name="memcapped",
            celery_worker_configs=[
                _celery_worker_config(max_memory_per_child_kib=655360)
            ],
        )
    )

    def check(containers):
        command = next(c for c in containers if c["name"] == "celery-worker")["command"]
        # celery takes the value as a separate argv entry, not --flag=value
        assert command[command.index("--max-memory-per-child") + 1] == "655360"

    return app.celery_deployments[0].spec.template.spec.containers.apply(check)


def test_max_memory_per_child_rejects_non_positive():
    with pytest.raises(ValidationError):
        _celery_worker_config(max_memory_per_child_kib=0)
