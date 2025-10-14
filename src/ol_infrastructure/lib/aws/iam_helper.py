import json
import re
from typing import Any, Literal

from parliament import analyze_policy_string
from parliament.finding import Finding

IAM_POLICY_VERSION = "2012-10-17"

ADMIN_USERNAMES = [
    "cpatti",
    "dansubak",
    "ferdial",
    "ichuang",
    "mas48",
    "pdpinch",
    "qhoque",
    "shaidar",
    "tmacey",
]

EKS_ADMIN_USERNAMES = [
    "cpatti",
    "dansubak",
    "mas48",
    "qhoque",
    "shaidar",
    "tmacey",
]

EKS_DEVELOPER_USERNAMES = [
    "ambady",
    "abeglova",
    "jkachel",
    "rlougee",
]


def _is_parliament_finding_filtered(
    finding: Finding, parliament_config: dict[str, Any]
) -> bool:
    issue_match = finding.issue in parliament_config
    if not issue_match:
        return False
    action_matches = []
    for location in parliament_config[finding.issue].get("ignore_locations", []):
        for action in location.get("actions", []):
            matches = [
                re.findall(action, finding_action, re.IGNORECASE)
                for finding_action in finding.location["actions"]
            ]
            action_matches.append(any(matches))
    else:  # noqa: PLW0120
        action_matches.append("all")  # type: ignore[arg-type]
    return any(action_matches)


def lint_iam_policy(
    policy_document: str | dict[str, Any],
    stringify: bool = False,  # noqa: FBT001, FBT002
    parliament_config: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Lint the contents of an IAM policy and abort execution if issues are found.

    :param policy_document: An IAM policy document represented as a JSON encoded string
        or a dictionary
    :type policy_document: Union[Text, dict[Text, Any]]

    :param stringify: If set to true then the dictionary of the policy document will be
        returned as a JSON string.
    :type stringify: bool

    :param parliament_config: A configuration object to customize the strictness and
        error checking of the Parliament library.
    :type parliament_config: dict

    :raises Exception: If there are linting violations detected then a bare exception is
        raised with the findings.

    :returns: The contents of the policy document that is passed to the function.

    :rtype: Union[Text, dict[Text, Any]]
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
        msg = "Potential issues found with IAM policy document"
        raise Exception(msg, findings)  # noqa: TRY002
    return (
        stringified_document if stringify and stringified_document else policy_document
    )


def route53_policy_template(zone_id: str) -> dict[str, Any]:
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


def oidc_trust_policy_template(
    oidc_identifier: str,
    account_id: str,
    k8s_service_account_identifier: str | list[str],
    operator: Literal["StringLike", "StringEquals"],
) -> dict[str, Any]:
    """Policy definition to allow EBS CSI driver installed into a EKS cluster
    to provision EBS resources

    :param oidc_identifier: The OIDC identifier from the cluster output prefixed
     with 'https://'
    :type oidc_identifier: str
    :param account_id: The numerical account identifier
    :type account_id: str
    :param k8s_service_account_identifier: The service account identifier(s) to apply
     to the :sub condition. Can be a single string or a list of strings.
    :type k8s_service_account_identifier: str | list[str]
    :param operator: Which string operator to use inside the conditional expression.
     vaild choices are "StringLike" and "StringEquals"
    :type operator: str

    :returns: A dictionary object representing a policy document to allow an EBS
     CSI driver installed into an EKS cluster to provision storage.
    """
    stripped_oidc_identifier = oidc_identifier.replace("https://", "")

    # Convert single identifier to list for uniform handling
    identifiers = (
        [k8s_service_account_identifier]
        if isinstance(k8s_service_account_identifier, str)
        else k8s_service_account_identifier
    )

    # For multiple identifiers, create separate statements
    statements = [
        {
            "Effect": "Allow",
            "Principal": {
                "Federated": f"arn:aws:iam::{account_id}:oidc-provider/{stripped_oidc_identifier}"  # noqa: E501
            },
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                f"{operator}": {
                    f"{stripped_oidc_identifier}:aud": "sts.amazonaws.com",
                    f"{stripped_oidc_identifier}:sub": f"{identifier}",
                }
            },
        }
        for identifier in identifiers
    ]

    return {
        "Version": IAM_POLICY_VERSION,
        "Statement": statements,
    }
