import pytest
from pydantic import ValidationError

from ol_infrastructure.lib import ol_types
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    Application,
    AWSBase,
    BusinessUnit,
    Component,
    DeploymentEnvironment,
    K8sAppLabels,
    K8sGlobalLabels,
    Product,
    Services,
    cluster_addon_labels,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

VALID_TAGS = {"OU": "operations", "Environment": "test"}


def test_tag_validation():
    with pytest.raises(ValueError):  # noqa: PT011
        AWSBase(tags={"foo": "bar", "Environment": "test"})
    with pytest.raises(ValueError):  # noqa: PT011
        AWSBase(tags={"foo": "bar", "OU": "test"})
    with pytest.raises(ValidationError):
        AWSBase(tags={"Environment": "test", "OU": "test"})


def test_region_validation(monkeypatch):
    # AWSBase.check_region calls aws_regions(), which hits ec2:DescribeRegions.
    # Stubbed so the assertion is about the validator rather than about whether
    # this process happens to hold AWS credentials.
    monkeypatch.setattr(
        ol_types, "aws_regions", lambda: ["us-east-1", "us-east-2", "us-west-2"]
    )
    AWSBase(tags=VALID_TAGS, region="us-east-1")
    with pytest.raises(ValueError):  # noqa: PT011
        AWSBase(tags=VALID_TAGS, region="us-east-0")


def test_merged_tags():
    base_config = AWSBase(tags=VALID_TAGS)
    new_tags = base_config.merged_tags({"Foo": "bar"})
    assert new_tags == {
        "OU": "operations",
        "Environment": "test",
        "Foo": "bar",
        "pulumi_managed": "true",
    }


def test_pulumi_managed_tag():
    base_config = AWSBase(tags=VALID_TAGS)
    assert base_config.tags.pop("pulumi_managed") == "true"


@pytest.fixture
def stack_info() -> StackInfo:
    return StackInfo(
        name="QA",
        namespace="",
        env_suffix="qa",
        env_prefix="",
        full_name="organization/ol-application-example/QA",
        k8s_name="ol-application-example.QA",
        project_name="ol-application-example",
    )


def test_global_labels_omit_unset_routing_fields(stack_info):
    """A caller that sets nothing new renders exactly what it did before."""
    labels = K8sGlobalLabels(
        ou=BusinessUnit.data,
        service=Services.dagster,
        stack=stack_info,
    ).model_dump()
    assert labels == {
        "ol.mit.edu/ou": "data",
        "ol.mit.edu/service": "dagster",
        "ol.mit.edu/stack": "ol-application-example.QA",
        "ol.mit.edu/environment": "qa",
    }


def test_global_labels_carry_routing_fields(stack_info):
    labels = K8sGlobalLabels(
        ou=BusinessUnit.data,
        service=Services.dagster,
        stack=stack_info,
        product=Product.data,
        application=Application.dagster,
        component=Component.pgbouncer,
        alert_tier=AlertTier.page,
    ).model_dump()
    assert labels["ol.mit.edu/product"] == "data"
    assert labels["ol.mit.edu/application"] == "dagster"
    assert labels["ol.mit.edu/component"] == "pgbouncer"
    assert labels["ol.mit.edu/alert_tier"] == "page"


@pytest.mark.parametrize("field", ["alert_tier", "component", "environment"])
def test_routing_fields_reject_arbitrary_strings(stack_info, field):
    """The label vocabularies are enforced, not merely documented.

    Rootly routes on these values, so a typo has to fail here rather than
    render a label that silently matches no routing rule.
    """
    with pytest.raises(ValidationError):
        K8sGlobalLabels(
            ou=BusinessUnit.data,
            service=Services.dagster,
            stack=stack_info,
            **{field: "not-a-member"},
        )


def test_environment_defaults_to_the_stack_suffix(stack_info):
    labels = K8sGlobalLabels(
        ou=BusinessUnit.data, service=Services.dagster, stack=stack_info
    ).model_dump()
    assert labels["ol.mit.edu/environment"] == "qa"


def test_environment_override_wins_over_the_stack_suffix(stack_info):
    """A workload whose stage differs from its cluster's must be able to say so.

    The mitx-staging deployments run in residential-production; without this
    they inherit the cluster's answer and are alerted on as production.
    """
    labels = K8sGlobalLabels(
        ou=BusinessUnit.residential,
        service=Services.openedx,
        stack=stack_info,
        environment=DeploymentEnvironment.staging,
    ).model_dump()
    assert labels["ol.mit.edu/environment"] == "staging"


def test_app_labels_still_require_the_stricter_contract(stack_info):
    with pytest.raises(ValidationError) as exc_info:
        K8sAppLabels(
            ou=BusinessUnit.mit_learn, service=Services.mit_learn, stack=stack_info
        )
    assert sorted(error["loc"][0] for error in exc_info.value.errors()) == [
        "application",
        "product",
        "source_repository",
    ]


def test_app_labels_sanitize_the_source_repository(stack_info):
    labels = K8sAppLabels(
        ou=BusinessUnit.mit_learn,
        service=Services.mit_learn,
        stack=stack_info,
        product=Product.mitlearn,
        application=Application.mit_learn,
        component=Component.frontend,
        source_repository="https://github.com/mitodl/mit-learn",
    ).model_dump()
    assert labels["ol.mit.edu/source_repository"] == "github.com_mitodl_mit-learn"


def test_addon_labels_render_the_full_routing_set(stack_info):
    labels = cluster_addon_labels(
        base_labels={"pulumi_managed": "true"},
        stack_info=stack_info,
        service=Services.cert_manager,
        component=Component.controller,
        alert_tier=AlertTier.notify,
    )
    assert labels == {
        # Platform engineering owns the addons, whatever cluster they run in.
        "ol.mit.edu/ou": "operations",
        "ol.mit.edu/service": "cert-manager",
        "ol.mit.edu/component": "controller",
        "ol.mit.edu/alert_tier": "notify",
        "ol.mit.edu/stack": "ol-application-example.QA",
        "ol.mit.edu/environment": "qa",
        "pulumi_managed": "true",
    }


def test_addon_labels_omit_a_role_the_caller_cannot_state(stack_info):
    """A chart key covering several roles publishes only what is true of all.

    The VPA and KEDA charts each expose one label key that reaches Deployments
    with different roles and, for VPA, different tiers. Emitting a shared
    component or tier there would mis-route the odd one out; emitting neither
    leaves those workloads on the severity catch-all.
    """
    labels = cluster_addon_labels(
        base_labels={},
        stack_info=stack_info,
        service=Services.vertical_pod_autoscaler,
    )
    assert "ol.mit.edu/component" not in labels
    assert "ol.mit.edu/alert_tier" not in labels
    assert labels["ol.mit.edu/service"] == "vertical-pod-autoscaler"


def test_addon_labels_never_overwrite_the_program_labels(stack_info):
    """The base dict wins, so a shared label keeps the value it already had.

    substructure/aws/eks sets its own ol.mit.edu/stack; rewriting it would
    churn every addon's pod template for a value that means the same thing.
    """
    labels = cluster_addon_labels(
        base_labels={"ol.mit.edu/stack": "substructure.aws.eks.operations.Production"},
        stack_info=stack_info,
        service=Services.karpenter,
        component=Component.controller,
        alert_tier=AlertTier.page,
    )
    assert labels["ol.mit.edu/stack"] == "substructure.aws.eks.operations.Production"
    assert labels["ol.mit.edu/alert_tier"] == "page"
