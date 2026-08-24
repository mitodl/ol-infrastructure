from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION

# Kept in step with DEFAULT_ACCESS_ROLE_ARN in bin/analyze-pulumi-iam-usage,
# which is the caller that passes it.
ACCESS_ANALYZER_SERVICE_ROLE_ARN = (
    "arn:aws:iam::*:role/service-role/"
    "AccessAnalyzerMonitorServiceRole_XELK4E2HVP"  # pragma: allowlist secret
)

# AWS Permissions Document
# Read-only IAM Access Analyzer access for the scheduled `iam-policy-drift`
# pipeline, which runs the `analyze-pulumi-iam-usage` tool on the infra worker
# pool to diff this pool's real API usage against what its policies grant. The
# pipeline definition lives under the iam_drift directory of this repo's
# infrastructure Concourse pipelines.
#
# Split out from pulumi_infra rather than folded into it because it is not
# Pulumi's usage: pulumi_infra is meant to stay a faithful record of what the
# stacks themselves call, and it is the module the drift check proposes edits
# to. Mixing the analysis tool's own permissions into the thing being analyzed
# would make that record self-referential.
policy_definition = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                # bin/analyze-pulumi-iam-usage calls the V2 finding APIs
                # (get_finding_v2/list_findings_v2), but AWS authorizes both
                # against the original (non-"V2") action names -- confirmed by
                # a live AccessDeniedException naming "access-analyzer:
                # ListFindings" for a ListFindingsV2 call, and by AWS's action
                # reference, which has no *FindingV2 action at all.
                "access-analyzer:GetFinding",
                "access-analyzer:GetGeneratedPolicy",
                "access-analyzer:ListFindings",
                "access-analyzer:StartPolicyGeneration",
            ],
            "Resource": "*",
        },
        {
            # StartPolicyGeneration reads CloudTrail on the caller's behalf via
            # a service role, so it needs PassRole on exactly that role and
            # nothing else. The condition keeps the grant useless for anything
            # other than handing this role to Access Analyzer.
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": ACCESS_ANALYZER_SERVICE_ROLE_ARN,
            "Condition": {
                "StringEquals": {"iam:PassedToService": "access-analyzer.amazonaws.com"}
            },
        },
    ],
}
