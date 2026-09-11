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

# No head sampling anywhere. Every service that starts or forwards a request is
# a root for something downstream -- APISIX and ToolHive literally so, edxapp
# and Keycloak for anything they call -- and `parentbased_*` means a downstream
# service honours the root's decision rather than re-rolling it. So a fractional
# rate here is not "sample this service", it is "discard that fraction of every
# end-to-end trace in the system", taken at the point with the least information
# about whether the trace turned out to be interesting.
#
# The Grafana Alloy tail sampler is the only thing that sees a whole trace, so
# it owns the decision: keep errors, keep slow traces, keep everything from
# services that are not high-volume, and sample the remainder (see
# substructure/aws/eks/grafana.py). Head sampling upstream of it only removes
# traces it never gets to judge.
DEFAULT_TRACE_SAMPLING_RATE = "1.0"


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
