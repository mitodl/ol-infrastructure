"""bedrock_invoke_statements must produce a policy that passes lint as documented."""

import pytest

from ol_infrastructure.lib.aws.iam_helper import (
    BEDROCK_PARLIAMENT_CONFIG,
    IAM_POLICY_VERSION,
    bedrock_invoke_statements,
    lint_iam_policy,
)

ACCOUNT_ID = "123456789012"


@pytest.mark.parametrize("vendor", [None, "anthropic"])
def test_statements_pass_lint_with_documented_config(vendor):
    policy = {
        "Version": IAM_POLICY_VERSION,
        "Statement": bedrock_invoke_statements(ACCOUNT_ID, vendor=vendor),
    }
    lint_iam_policy(policy, parliament_config=BEDROCK_PARLIAMENT_CONFIG)


def test_grants_no_marketplace_actions():
    # Subscribing enables a third-party model, and accepts its EULA, for the
    # whole account; that is an administrator's call, not a workload's.
    actions = [
        action
        for statement in bedrock_invoke_statements(ACCOUNT_ID)
        for action in statement["Action"]
    ]
    assert not [action for action in actions if action.startswith("aws-marketplace")]


def test_unscoped_allows_every_vendor():
    invoke = bedrock_invoke_statements(ACCOUNT_ID)[0]
    assert invoke["Resource"] == [
        "arn:aws:bedrock:*::foundation-model/*",
        f"arn:aws:bedrock:*:{ACCOUNT_ID}:inference-profile/*",
    ]


def test_vendor_scopes_models_and_cross_region_profiles():
    invoke = bedrock_invoke_statements(ACCOUNT_ID, vendor="anthropic")[0]
    # The profile glob has to match the region-prefixed IDs Bedrock requires for
    # on-demand invocation of newer models, e.g. us.anthropic.claude-sonnet-5.
    assert invoke["Resource"] == [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        f"arn:aws:bedrock:*:{ACCOUNT_ID}:inference-profile/*anthropic*",
    ]
