import json
from typing import Any, Dict, Text, Union

from parliament import analyze_policy_string


def lint_iam_policy(
    policy_document: Union[Text, Dict[Text, Any]], stringify: bool = False
) -> Union[Text, Dict[Text, Any]]:
    """Lint the contents of an IAM policy and abort execution if issues are found.

    :param policy_document: An IAM policy document represented as a JSON encoded string or a dictionary
    :type policy_document: Union[Text, Dict[Text, Any]]

    :param stringify: If set to true then the dictionary of the policy document will be returned as a JSON string.
    :type stringify: bool

    :raises Exception: If there are linting violations detected then a bare exception is raised with the findings.

    :returns: The contents of the policy document that is passed to the function.

    :rtype: Union[Text, Dict[Text, Any]]
    """
    stringified_document = None
    if not isinstance(policy_document, Text):
        stringified_document = json.dumps(policy_document)
    findings = analyze_policy_string(stringified_document or policy_document).findings
    if findings:
        raise Exception("Potential issues found with IAM policy document", findings)
    return (
        stringified_document if stringify and stringified_document else policy_document
    )


def route53_policy_template(zone_id: Text) -> Dict[Text, Any]:
    """Policy definition to allow Caddy to use Route 53 to resolve DNS challenges."""
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
