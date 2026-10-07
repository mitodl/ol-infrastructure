from ol_infrastructure.lib.jupyterhub_config import jupyterhub_workload_labels
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

TIER = "ol.mit.edu/alert_tier"
COMPONENT = "ol.mit.edu/component"


def _labels() -> K8sGlobalLabels:
    return K8sGlobalLabels(
        service=Services.notebooks,
        ou=BusinessUnit.data,
        stack=StackInfo(
            name="QA",
            namespace="",
            env_suffix="qa",
            env_prefix="",
            full_name="organization/ol-application-example/QA",
            k8s_name="ol-application-example.QA",
            project_name="ol-application-example",
        ),
    )


def test_serving_tier_applies_to_hub_and_proxy_only():
    labels = jupyterhub_workload_labels(_labels(), AlertTier.page)

    assert {section: values[TIER] for section, values in labels.items()} == {
        "hub": "page",
        "proxy": "page",
        "user_scheduler": "notify",
        "singleuser": "notify",
        "user_placeholder": "ticket",
        "pre_puller": "ticket",
    }


def test_each_section_names_its_component():
    labels = jupyterhub_workload_labels(_labels(), AlertTier.notify)

    assert {section: values[COMPONENT] for section, values in labels.items()} == {
        "hub": "webapp",
        "proxy": "gateway",
        "user_scheduler": "controller",
        "singleuser": "worker",
        "user_placeholder": "worker",
        "pre_puller": "agent",
    }


def test_every_section_keeps_the_stack_labels():
    base = _labels().model_dump()

    for values in jupyterhub_workload_labels(_labels(), AlertTier.notify).values():
        assert base.items() <= values.items()
