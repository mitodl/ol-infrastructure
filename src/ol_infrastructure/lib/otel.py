"""Shared OTLP endpoint + shipping decision for every stack that exports OTel.

── Why CI gets logs but no traces or metrics ──
``setup_grafana`` (``substructure/aws/eks/grafana.py``) returns early for CI, so
operations-ci runs no Grafana Alloy: its ``grafana`` namespace exists and is
empty, and ``grafana-k8s-monitoring-alloy-receiver`` resolves in QA and
Production only. ``ships_telemetry`` mirrors that condition, so CI lands on the
no-exporter path by construction instead of buying a connection failure per
batch forever in the one environment nobody is watching.
"""

from ol_infrastructure.lib.pulumi_helper import StackInfo

# The in-cluster OTLP/HTTP receiver, shared with mit_learn, learn_ai and edxapp
# (see their Pulumi.{QA,Production}.yaml and edxapp/k8s_configmaps.py). Port
# 4318 is http/protobuf; 4317 on the same Service is gRPC, which we do not use.
OTLP_ENDPOINT = (
    "http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318"
)

# Matched to the mit_learn/learn_ai precedent rather than chosen fresh, so a
# trace crossing from one of those services is sampled consistently instead of
# being decided twice.
DEFAULT_TRACE_SAMPLING_RATE = "0.25"


def ships_telemetry(stack_info: StackInfo) -> bool:
    """Whether this environment has an OTLP receiver to export to.

    Mirrors ``setup_grafana``'s CI early-return. Kept as a named predicate so
    the reason a stack is dark is one grep away from the reason the collector
    is absent.

    The ``.lower()`` is redundant and deliberate: ``parse_stack`` builds
    ``env_suffix`` as ``stack_name.lower()``, so it is lowercase by
    construction. It is kept only so this predicate is character-for-character
    the condition ``setup_grafana`` tests — the two drifting apart is the
    failure this function exists to prevent.
    """
    return stack_info.env_suffix.lower() != "ci"
