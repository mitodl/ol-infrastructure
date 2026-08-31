import json
import re
from typing import Any, Literal

from parliament import analyze_policy_string
from parliament.finding import Finding

IAM_POLICY_VERSION = "2012-10-17"

# All users with admin access to AWS
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

# DevOps team members
DEVOPS_ADMIN_USERNAMES = [
    "cpatti",
    "mas48",
    "qhoque",
    "shaidar",
    "tmacey",
]

# DevOps team members plus some special extras
EKS_ADMIN_USERNAMES = [
    "cpatti",
    "dansubak",
    "mas48",
    "qhoque",
    "shaidar",
    "tmacey",
]

# Unused
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


def _json_byte_size(document: dict[str, Any]) -> int:
    """Return the UTF-8 byte size of a document's compact JSON serialization.

    IAM's policy size quota is enforced on the document's byte size, not its
    character count -- these differ once any non-ASCII character appears.
    """
    return len(json.dumps(document).encode("utf-8"))


def _split_statement_by_action(
    base: dict[str, Any],
    actions: list[str],
    action_budget: int,
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Split one statement's Action list into pieces that fit action_budget.

    Each returned piece is base (Effect/Resource/Condition) plus a slice of
    actions, small enough to serialize within action_budget on its own.

    :raises ValueError: If a single action, together with base, is too large
        to fit on its own -- there's no further way to split it.
    """
    pieces: list[dict[str, Any]] = []
    current: list[str] = []
    for action in actions:
        candidate = [*current, action]
        if _json_byte_size({**base, "Action": candidate}) <= action_budget:
            current = candidate
            continue
        if current:
            pieces.append({**base, "Action": current})
            current = []
        single_size = _json_byte_size({**base, "Action": [action]})
        if single_size > action_budget:
            msg = (
                f"IAM action {action!r} alone (with its statement's "
                f"Effect/Resource/Condition) is {single_size} bytes, which "
                f"exceeds the {action_budget}-byte per-statement budget "
                f"under max_bytes={max_bytes}; it cannot be split further."
            )
            raise ValueError(msg)
        current = [action]
    if current:
        pieces.append({**base, "Action": current})
    return pieces


def split_iam_policy_by_size(
    policy_definition: dict[str, Any],
    max_bytes: int = 6144,
) -> list[dict[str, Any]]:
    """Split a policy document into multiple documents that each fit max_bytes.

    IAM enforces a hard, non-adjustable 6,144 byte cap on a single managed
    policy document. A role can have several managed policies attached, so a
    document that would exceed the cap can instead be split into siblings that
    are all attached to the same role, each carrying its own copy of a
    statement's Effect/Resource/Condition and a slice of its Action list.

    :param policy_definition: An IAM policy document (Version + Statement).
    :param max_bytes: The per-document size ceiling to split against.

    :raises ValueError: If a single action, together with its statement's
        Effect/Resource/Condition, is too large to fit in any document on its
        own -- there's no further way to split it.

    :returns: One or more policy documents, each within max_bytes when
        JSON-serialized, that together grant everything policy_definition does.
    """
    version = policy_definition["Version"]
    envelope_size = _json_byte_size({"Version": version, "Statement": []})
    action_budget = max_bytes - envelope_size

    pieces: list[dict[str, Any]] = []
    for statement in policy_definition["Statement"]:
        actions = statement["Action"]
        if isinstance(actions, str):
            actions = [actions]
        base = {key: value for key, value in statement.items() if key != "Action"}
        pieces.extend(
            _split_statement_by_action(base, actions, action_budget, max_bytes)
        )

    documents: list[list[dict[str, Any]]] = []
    for piece in pieces:
        for document_statements in documents:
            candidate_size = _json_byte_size(
                {"Version": version, "Statement": [*document_statements, piece]}
            )
            if candidate_size <= max_bytes:
                document_statements.append(piece)
                break
        else:
            documents.append([piece])

    return [
        {"Version": version, "Statement": document_statements}
        for document_statements in documents
    ]


def route53_policy_template(zone_id: str | list[str]) -> dict[str, Any]:
    """Policy definition granting write access to one or more Route53 zones.

    This provides the permissions necessary to modify Route53 records, for example in a
    Caddy configuration that is using the DNS authorization method for Let's Encrypt.

    :param zone_id: The ID, or list of IDs, of the DNS zone(s) the policy is being
        generated for.
    :type zone_id: str | list[str]

    :returns: A dictionary object representing a policy document to allow access to
              modify records in a Route53 zone.
    """
    zone_ids = [zone_id] if isinstance(zone_id, str) else zone_id
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
                "Resource": [f"arn:aws:route53:::hostedzone/{zid}" for zid in zone_ids]
                + ["arn:aws:route53:::change/*"],
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


# Glue database naming conventions for the OL data lake, per environment.
#
# Anchored on purpose. These were previously a bare substring match --
# ``database/*{env_suffix}*`` -- which grants an environment access to any
# database whose name happens to contain its own name anywhere. That is not a
# scoping rule, it is a coincidence of naming: the QA identities reached
# ``ol_warehouse_production_qa_{external,intermediate,mart,staging}``, dbt
# developer schemas in the *production* namespace, one of them registered under
# ``s3://ol-data-lake-staging-production/``.
#
# "ci" makes the same failure far likelier than "qa" does, being two characters
# that appear inside ordinary words: a future
# ``ol_warehouse_production_pricing_mart`` would be reachable by the CI
# identities.
DATA_LAKE_GLUE_NAMESPACES = (
    # dbt writes ol_warehouse_{env}_{layer}, plus ol_warehouse_{env}_{user}_{layer}
    # for developer and branch schemas.
    "ol_warehouse_{env_suffix}",
    "ol_warehouse_{env_suffix}_*",
    # The raw landing databases exist in both separator styles.
    "ol_data_lake_*_{env_suffix}",
    "ol-data-lake-*-{env_suffix}",
)

# Environments that lower environments must never reach into, whatever the Allow
# statements happen to say. Deliberately one-directional: the risk being managed
# is QA or CI writing to production, not the reverse, and denying the production
# identities access to QA would break developers whose DAGSTER_ENV=dev targets
# the production dbt profile.
PROTECTED_DATA_LAKE_ENVIRONMENTS = ("production",)

# Glue resource types the data lake identities act on, with the suffix each ARN
# needs after the database name.
_GLUE_RESOURCE_SUFFIXES = {
    "database": "",
    "table": "/*",
    "userDefinedFunction": "/*",
}


def data_lake_glue_namespaces(env_suffix: str) -> list[str]:
    """Database-name patterns belonging to one environment's data lake.

    :param env_suffix: The environment being scoped, e.g. ``qa``.
    :type env_suffix: str

    :returns: Glue database name patterns, anchored so that another
              environment's namespace cannot match.

    :rtype: list[str]
    """
    return [
        namespace.format(env_suffix=env_suffix)
        for namespace in DATA_LAKE_GLUE_NAMESPACES
    ]


def data_lake_glue_resources(
    env_suffix: str,
    resource_types: tuple[str, ...] = ("database", "table"),
) -> list[str]:
    """Glue ARNs scoped to one environment's data lake namespaces.

    :param env_suffix: The environment being scoped, e.g. ``qa``.
    :type env_suffix: str

    :param resource_types: Which Glue resource types to emit ARNs for. Defaults
        to databases and tables; the query engine passes
        ``userDefinedFunction`` as well, because it manages those too.
    :type resource_types: tuple[str, ...]

    :returns: A resource list for an IAM statement, including the catalog ARN
              that every Glue call needs.

    :rtype: list[str]
    """
    namespaces = data_lake_glue_namespaces(env_suffix)
    return ["arn:aws:glue:*:*:catalog"] + [
        f"arn:aws:glue:*:*:{resource_type}/{namespace}"
        f"{_GLUE_RESOURCE_SUFFIXES[resource_type]}"
        for resource_type in resource_types
        for namespace in namespaces
    ]


def cross_environment_glue_denial(env_suffix: str) -> list[dict[str, Any]]:
    """Deny a lower environment any Glue action on a protected environment.

    Defence in depth rather than the primary control -- ``data_lake_glue_resources``
    already declines to grant it. This exists because an explicit Deny cannot be
    undone by a later Allow, and widening a grant for some unrelated reason is
    the way this would realistically regress.

    Attach the result to the principal being constrained, and never to a policy
    that is shared across environments. A Deny applies to whichever principal
    holds the document, not to the environment that generated it, so a shared
    policy carries it onto every role it is attached to. That is how the
    production StarRocks role ended up holding QA's "deny production" statement
    and losing the production Glue catalog: an explicit Deny cannot be undone by
    the Allow in the production policy attached beside it.

    Ship it as a dedicated managed policy attached to those principals, not as an
    inline ``iam.RolePolicy``. The Concourse deploy role is granted
    ``iam:CreatePolicy``, ``iam:CreatePolicyVersion`` and ``iam:AttachRolePolicy``
    but not ``iam:PutRolePolicy``, so an inline policy fails from CI with
    AccessDenied while the surrounding managed-policy update succeeds -- which
    publishes the Allow and strands the environment without the Deny. Nothing
    local catches this: a ``pulumi preview`` performs no writes, so it cannot
    surface a write-permission gap, and a developer applying by hand would not
    hit it either, since developer credentials do carry ``iam:PutRolePolicy``.
    Check what the deploy role can actually do before reaching for a new IAM
    resource type here.

    ``arn:aws:glue:*:*:catalog`` is deliberately absent, and must stay absent.
    Every Glue operation requires permission on the catalog, so denying it would
    deny every Glue call these identities make -- including the ones against
    their own databases -- rather than hardening anything. Confirmed with the
    IAM policy simulator: adding it turns ``glue:GetTable`` on the catalog from
    ``allowed`` into ``explicitDeny``. The catalog belongs in the Allow, which is
    where ``data_lake_glue_resources`` puts it.

    :param env_suffix: The environment the policy is being generated for.
    :type env_suffix: str

    :returns: A single Deny statement, or an empty list when the environment is
              itself protected and so has nothing to be kept out of.

    :rtype: list[dict[str, Any]]
    """
    protected = [
        environment
        for environment in PROTECTED_DATA_LAKE_ENVIRONMENTS
        if environment != env_suffix
    ]
    if not protected:
        return []
    return [
        {
            "Sid": "DenyCrossEnvironmentGlueAccess",
            "Effect": "Deny",
            "Action": "glue:*",
            # Built from the same namespaces the Allow is built from, so the two
            # cannot fall out of step. Denying only ``ol_warehouse_production*``
            # would leave the raw landing databases -- ``ol_data_lake_*_production``
            # and ``ol-data-lake-*-production`` -- uncovered, which is exactly the
            # gap this statement exists to close.
            "Resource": [
                f"arn:aws:glue:*:*:{resource_type}/{namespace}{suffix}"
                for environment in protected
                for namespace in data_lake_glue_namespaces(environment)
                for resource_type, suffix in _GLUE_RESOURCE_SUFFIXES.items()
            ],
        }
    ]
