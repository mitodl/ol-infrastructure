import json
import re
from typing import Any, Dict, Union

from parliament import analyze_policy_string
from parliament.finding import Finding

IAM_POLICY_VERSION = "2012-10-17"


def _is_parliament_finding_filtered(
    finding: Finding, parliament_config: Dict[str, Any]
) -> bool:
    issue_match = finding.issue in parliament_config.keys()
    if not issue_match:
        return False
    action_matches = []
    for location in parliament_config[finding.issue].get(  # noqa: WPS352, WPS426
        "ignore_locations", []
    ):
        for action in location.get("actions", []):  # noqa: WPS426
            matches = map(
                lambda finding_action: re.findall(
                    action, finding_action, re.IGNORECASE
                ),
                finding.location["actions"],
            )
            action_matches.append(any(matches))
    else:
        action_matches.append("all")
    return any(action_matches)


def lint_iam_policy(
    policy_document: Union[str, Dict[str, Any]],
    stringify: bool = False,
    parliament_config: Dict = None,
) -> Union[str, Dict[str, Any]]:
    """Lint the contents of an IAM policy and abort execution if issues are found.

    :param policy_document: An IAM policy document represented as a JSON encoded string
        or a dictionary
    :type policy_document: Union[Text, Dict[Text, Any]]

    :param stringify: If set to true then the dictionary of the policy document will be
        returned as a JSON string.
    :type stringify: bool

    :param parliament_config: A configuration object to customize the strictness and
        error checking of the Parliament library.
    :type parliament_config: Dict

    :raises Exception: If there are linting violations detected then a bare exception is
        raised with the findings.

    :returns: The contents of the policy document that is passed to the function.

    :rtype: Union[Text, Dict[Text, Any]]
    """
    stringified_document = None
    if not isinstance(policy_document, str):
        stringified_document = json.dumps(policy_document)
    findings = analyze_policy_string(
        stringified_document or policy_document,
        include_community_auditors=True,
        config=parliament_config,
    ).findings
    findings = [
        finding
        for finding in findings
        if not _is_parliament_finding_filtered(finding, parliament_config or {})
    ]
    if findings:
        raise Exception(  # noqa: WPS454
            "Potential issues found with IAM policy document", findings
        )
    return (
        stringified_document if stringify and stringified_document else policy_document
    )


def route53_policy_template(zone_id: str) -> Dict[str, Any]:
    """Policy definition to allow Caddy to use Route 53 to resolve DNS challenges.

    This provides the permissions necessary to modify Route53 records, for example in a
    Caddy configuration that is using the DNS authorization method for Let's Encrypt.

    :param zone_id: The ID of the DNS zone that the policy is being generated for.
    :type zone_id: str

    :returns: A dictionary object representing a policy document to allow access to
              modify records in a Route53 zone.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "route53:ListResourceRecordSets",
                    "route53:GetChange",
                    "route53:ChangeResourceRecordSets",
                ],
                "Resource": [
                    f"arn:aws:route53:::hostedzone/{zone_id}",
                    "arn:aws:route53:::change/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["route53:ListHostedZonesByName", "route53:ListHostedZones"],
                "Resource": "*",
            },
        ],
    }
