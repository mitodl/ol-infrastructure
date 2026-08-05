"""The org custom-property schema that rulesets target.

THIS IS THE ONE THING PHASE 2 CREATES RATHER THAN IMPORTS. The org has no custom
properties today, so `tier` is genuinely new -- every other resource in this project
is an import of something that already exists. The first preview shows one create;
every preview after that is empty.

`OrganizationCustomProperties` is one resource PER PROPERTY despite the plural class
name; its only required input is `property_name`. Verified against the shipped
provider schema rather than inferred from the name.

SEQUENCING HAZARD, and the reason this file is worth reading before phase 3.5:

`required=True` plus `default_value` applies the property to every repo that has no
explicit value. Verified live on 2026-08-05 against three repos that had never been
labelled -- all three immediately reported `standard`.

So the moment this lands, ALL 316 repos carry `tier=standard`, including the 102
forks and 140 archived repos meant to end up `unmanaged`. That is harmless today
only because no ruleset exists yet. It stops being harmless the instant
`baseline-default-branch` is created, since that ruleset targets `standard`.

Required order, which the pipeline must encode rather than discover:

1. this project -- define `tier`; every repo becomes `standard`
2. repositories -- set explicit values; forks and archived become `unmanaged`
3. phase 3.5    -- create the rulesets, at `enforcement: evaluate` first

Creating a ruleset before step 2 would apply branch protection to the entire fork
fleet in one apply. The failure is invisible in preview: the ruleset diff looks
identical either way, because what changed is which repos MATCH it, and matching is
not a Pulumi resource.
"""

import pulumi_github as github
from pulumi import ResourceOptions

from ol_infrastructure.saas.github.tiers import (
    TIER_PROPERTY_NAME,
    TIER_STANDARD,
    TIER_VALUES,
)

tier_property = github.OrganizationCustomProperties(
    "mitodl-custom-property-tier",
    property_name=TIER_PROPERTY_NAME,
    value_type="single_select",
    # Required + default is what makes a NEW repo protected at creation (section 3.5).
    # Without the default a new repo carries no tier, matches no ruleset, and is
    # unprotected until somebody notices -- the exact failure `ol-django` shows today.
    required=True,
    default_value=TIER_STANDARD,
    allowed_values=list(TIER_VALUES),
    description=(
        "Governance tier. Org rulesets target this property; `unmanaged` is "
        "deliberately untargeted. Set from ol-saas-github-repositories."
    ),
    # Only org admins may change a tier. Values are set by the repositories project;
    # a repo owner re-tiering out of the baseline would be a silent downgrade.
    values_editable_by="org_actors",
    opts=ResourceOptions(protect=True),
)
