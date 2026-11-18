"""The archive writers' shared facts stay true to what they describe.

The EC2 Vector sinks are written by cloud-init beside an image's templates and
name those templates' transforms as inputs. A renamed transform would fail
Vector's config load on the next instance launch and take the Grafana Cloud
sinks down with it, and the image build's `vector validate` cannot see a file
that is not in the image. This is the check that stands in for it.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

import bilder
from ol_infrastructure.lib import telemetry_archive as ta

BILDER_DIR = Path(bilder.__file__).parent
IMAGES_DIR = BILDER_DIR / "images"
ALLOY_FILES_DIR = BILDER_DIR / "components" / "alloy" / "files"
HOST_METRICS_TEMPLATE = (
    BILDER_DIR / "components" / "vector" / "templates" / "host_metrics.yaml"
)
CONCOURSE_METRICS_TEMPLATE = (
    IMAGES_DIR / "concourse" / "templates" / "vector" / "concourse_metrics.yaml"
)


def _target() -> ta.ArchivePushTarget:
    return ta.ArchivePushTarget(
        domain="telemetry-archive-qa.ol.mit.edu", username="u", password="p"
    )


def _image_log_template(image: str) -> dict[str, Any]:
    return yaml.safe_load(
        (IMAGES_DIR / image / "templates" / "vector" / f"{image}_logs.yaml").read_text()
    )


@pytest.mark.parametrize("image", sorted(ta.EC2_VECTOR_LOG_PIPELINES))
def test_ec2_sink_inputs_are_transforms_in_the_image_template(image: str) -> None:
    """Vector refuses a sink whose input does not exist, taking Cloud down with it."""
    template = _image_log_template(image)
    pipeline = ta.EC2_VECTOR_LOG_PIPELINES[image]

    assert set(pipeline["inputs"]) <= set(template["transforms"])


@pytest.mark.parametrize("image", sorted(ta.EC2_VECTOR_LOG_PIPELINES))
def test_ec2_sink_mirrors_the_cloud_sink(image: str) -> None:
    """Same inputs and labels as the Cloud sink, so both hold the same streams."""
    template = _image_log_template(image)
    (cloud_sink,) = [
        sink for sink in template["sinks"].values() if sink["type"] == "loki"
    ]
    pipeline = ta.EC2_VECTOR_LOG_PIPELINES[image]

    assert sorted(pipeline["inputs"]) == sorted(cloud_sink["inputs"])
    assert pipeline["labels"] == cloud_sink["labels"]


def test_ec2_sink_file_is_a_bounded_loki_sink() -> None:
    """The archive leg drops rather than waits (ADR-0014)."""
    entry = ta.ec2_vector_sink_file("consul", _target())
    (sink,) = yaml.safe_load(entry["content"])["sinks"].values()

    assert entry["path"] == ta.EC2_VECTOR_LOG_SINK_PATH
    assert sink["type"] == "loki"
    assert sink["endpoint"] == "https://telemetry-archive-qa.ol.mit.edu"
    assert sink["buffer"]["when_full"] == "drop_newest"
    assert sink["healthcheck"] is False
    assert sink["request"]["retry_attempts"] == ta.VECTOR_SINK_RETRY_ATTEMPTS
    assert sink["auth"]["user"] == "${ARCHIVE_PUSH_USERNAME}"


def _metric_template_transforms(role: str) -> set[str]:
    templates = [HOST_METRICS_TEMPLATE]
    if role == "concourse-web":
        templates.append(CONCOURSE_METRICS_TEMPLATE)
    return {
        transform
        for template in templates
        for transform in yaml.safe_load(template.read_text())["transforms"]
    }


@pytest.mark.parametrize("role", sorted(ta.EC2_VECTOR_METRIC_INPUTS))
def test_ec2_metric_sink_inputs_are_transforms_the_role_installs(role: str) -> None:
    """Every image has host_metrics.yaml; only Concourse web adds the exporter."""
    assert set(ta.EC2_VECTOR_METRIC_INPUTS[role]) <= _metric_template_transforms(role)


def test_ec2_metric_sink_mirrors_the_cloud_sinks() -> None:
    """The archive receives exactly the transforms the Cloud remote-write sinks read."""
    cloud_inputs = {
        transform
        for template in (HOST_METRICS_TEMPLATE, CONCOURSE_METRICS_TEMPLATE)
        for sink in yaml.safe_load(template.read_text())["sinks"].values()
        if sink["type"] == "prometheus_remote_write"
        for transform in sink["inputs"]
    }
    assert set(ta.EC2_VECTOR_METRIC_INPUTS["concourse-web"]) == cloud_inputs


def test_ec2_metric_sink_file_is_a_bounded_remote_write_sink() -> None:
    """Same bounds as the log sink; the credential is the one push credential."""
    entry = ta.ec2_vector_metric_sink_file("concourse-worker", _target())
    (sink,) = yaml.safe_load(entry["content"])["sinks"].values()

    assert entry["path"] == ta.EC2_VECTOR_METRIC_SINK_PATH
    assert entry["path"] != ta.EC2_VECTOR_LOG_SINK_PATH
    assert sink["type"] == "prometheus_remote_write"
    assert sink["endpoint"] == "https://telemetry-archive-qa.ol.mit.edu/api/v1/push"
    assert sink["inputs"] == [ta.HOST_METRICS_INPUT]
    assert sink["buffer"]["when_full"] == "drop_newest"
    assert sink["healthcheck"] is False
    assert sink["auth"]["user"] == "${ARCHIVE_PUSH_USERNAME}"


def test_env_lines_name_the_variables_the_configs_read() -> None:
    """/etc/default/* and the configs that read them agree on the variable names."""
    target = _target()
    vector_lines = ta.ec2_vector_env_lines(target)
    alloy_lines = ta.ec2_alloy_env_lines(target)

    assert "ARCHIVE_PUSH_USERNAME=u\n" in vector_lines
    assert 'ARCHIVE_OTLP_ENDPOINT="https://telemetry-archive-qa.ol.mit.edu/otlp"\n' in (
        alloy_lines
    )
    assert (
        'ARCHIVE_REMOTE_WRITE_ENDPOINT="https://telemetry-archive-qa.ol.mit.edu/api/v1/push"\n'
        in alloy_lines
    )
    for variable in (
        ta.ALLOY_OTLP_ENDPOINT_ENV,
        ta.ALLOY_REMOTE_WRITE_ENDPOINT_ENV,
        ta.PUSH_USERNAME_ENV,
        ta.PUSH_PASSWORD_ENV,
    ):
        assert f'sys.env("{variable}")' in ta.EC2_ALLOY_ARCHIVE_MODULE


def test_alloy_module_exports_what_the_image_config_imports() -> None:
    """config.alloy fans logs and metrics out to the two declares in archive.alloy."""
    image_config = (ALLOY_FILES_DIR / "config.alloy").read_text()
    image_default = (ALLOY_FILES_DIR / "archive.alloy").read_text()

    assert "archive.log_sink.default.input" in image_config
    assert "archive.metric_sink.default.receiver" in image_config
    assert f'filename = "{ta.EC2_ALLOY_MODULE_PATH}"' in image_config
    for module in (image_default, ta.EC2_ALLOY_ARCHIVE_MODULE):
        assert 'declare "log_sink"' in module
        assert 'export "input"' in module
        assert 'declare "metric_sink"' in module
        assert 'export "receiver"' in module


def test_only_qa_and_production_have_an_archive() -> None:
    """CI ships to Grafana Cloud alone (plan D1)."""
    assert ta.archive_domain("ci") is None
    assert ta.archive_domain("qa") == "telemetry-archive-qa.ol.mit.edu"
    assert ta.archive_domain("production") == "telemetry-archive.ol.mit.edu"
