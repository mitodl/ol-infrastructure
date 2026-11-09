"""OpenMetadata access policies, roles and teams, declared once.

The stack creates these with the access-control providers
(ol_infrastructure.providers.openmetadata.access_control), and the team sync
CronJob mirrors the teams into Keycloak groups.

A user's own roles come from the `roles` claim that Keycloak sends at login
(authorizer useRolesFromProvider in applications/open_metadata, mapping in
substructure/keycloak ol_data_platform.py OPEN_METADATA_ROLE_BY_REALM_ROLE):
ol-platform-admin and ol-data-engineer -> DataEngineer, ol-researcher ->
DataContributor, the two analyst roles and every other user -> DataConsumer.
No realm role maps to Admin; admins come from initialAdmins.  The claim
replaces the roles a user holds at login, and the team-keycloak-sync job
applies the same mapping every hour, so roles assigned in the UI to anyone but
an admin or a user with no Keycloak account do not survive the next run.  On
top of their own roles every user inherits DataConsumer and OrganizationPolicy
from the Organization team, and a team's members inherit its defaultRoles.

DataEngineer and DataContributor are the custom roles the claim names.  They
grant only what DataConsumer does not: DataConsumer already lets everyone
view everything and edit descriptions, tags, glossary terms, tier and
certification.
"""

from ol_infrastructure.providers.openmetadata.access_control import (
    OpenMetadataPolicyRule,
    OpenMetadataPolicySpec,
    OpenMetadataRoleSpec,
    OpenMetadataTeamSpec,
)

# Entity types that hold warehouse and BI metadata, as opposed to governance
# entities (glossaries, teams, policies).
_DATA_ASSET_RESOURCES: list[str] = [
    "apiEndpoint",
    "chart",
    "container",
    "dashboard",
    "dashboardDataModel",
    "database",
    "databaseSchema",
    "metric",
    "mlmodel",
    "pipeline",
    "searchIndex",
    "storedProcedure",
    "table",
    "topic",
]

POLICIES: list[OpenMetadataPolicySpec] = [
    # A Slack destination's webhook URL is stored and returned unmasked
    # (EventSubscriptionRepository only masks it on /testDestination), and the
    # default policies give everyone ViewAll on every resource, so anyone who
    # can log in could read the URL and post to the alert channel.  A deny rule
    # wins over every allow and admins skip policy evaluation, so denying All
    # leaves only admins able to read, edit or create alerts through
    # /v1/events/subscriptions.  It also blocks the description edit the
    # default DataConsumerPolicy allows, whose PATCH response carries the URL.
    #
    # Two reads no policy can close, so this keeps the URL from casual reads,
    # not from a determined user:
    # - A PATCH is authorized only for the operations its patch touches
    #   (JsonPatchUtils.getMetadataOperations), so an empty patch passes for any
    #   user and returns the whole alert.
    # - The change-event feed (GET /v1/events) is not permission-checked and
    #   keeps each alert's create and update, URL included, for the
    #   change-event retention period.
    OpenMetadataPolicySpec(
        name="HideAlertDestinationsPolicy",
        display_name="Hide Alert Destinations Policy",
        description=(
            "Keeps alerts and notifications, whose Slack webhook URLs are "
            "returned unmasked, out of the alerts API for every user but admins."
        ),
        rules=[
            OpenMetadataPolicyRule(
                name="HideAlertDestinationsPolicy-EventSubscriptions",
                description="Deny every operation on alerts and notifications.",
                effect="deny",
                resources=["eventsubscription"],
                operations=["All"],
            ),
        ],
    ),
    OpenMetadataPolicySpec(
        name="DataEngineerPolicy",
        display_name="Data Engineer Policy",
        description=(
            "Technical metadata, lineage and data quality tests on data assets."
        ),
        rules=[
            OpenMetadataPolicyRule(
                name="DataEngineerPolicy-TechnicalMetadata",
                description="Edit technical metadata and lineage of data assets.",
                effect="allow",
                resources=_DATA_ASSET_RESOURCES,
                operations=[
                    "EditCustomFields",
                    "EditDescription",
                    "EditDisplayName",
                    "EditLineage",
                    "EditTags",
                ],
            ),
            OpenMetadataPolicyRule(
                name="DataEngineerPolicy-DataQuality",
                description=(
                    "Add and edit the tests on a table, and triage their failures."
                ),
                effect="allow",
                resources=["table", "testCase", "testSuite"],
                operations=["CreateTests", "EditTests"],
            ),
            OpenMetadataPolicyRule(
                name="DataEngineerPolicy-TestCases",
                description="Create and edit test cases and test suites.",
                effect="allow",
                resources=["testCase", "testSuite"],
                operations=["Create", "EditAll"],
            ),
        ],
    ),
    OpenMetadataPolicySpec(
        name="DataContributorPolicy",
        display_name="Data Contributor Policy",
        description="Save queries against data assets.",
        rules=[
            # A saved query is owned by its author, so OrganizationPolicy's
            # owner rule covers editing and deleting it afterwards.
            OpenMetadataPolicyRule(
                name="DataContributorPolicy-Queries",
                description="Save SQL queries to data assets.",
                effect="allow",
                resources=["query"],
                operations=["Create"],
            ),
        ],
    ),
    # Exactly what ol-data-platform's asset_check_openmetadata_sensor (lakehouse
    # code location) needs: GET and, when missing, PUT the
    # dagsterAssetCheck test definition (ViewBasic, Create); GET and POST a test
    # case (ViewTests, CreateTests on its table); POST its results (EditTests on
    # the table).  Bots inherit no team policies.  OpenMetadata also gives every
    # bot DefaultBotRole, which cannot be removed, so the token can still view
    # everything and edit descriptions, tags, glossary terms, tier and
    # certification on any entity, and file tasks, through its
    # DataConsumerPolicy.
    OpenMetadataPolicySpec(
        name="DagsterBotPolicy",
        display_name="Dagster Bot Policy",
        description=(
            "Lets Dagster write its asset check results as test cases on tables."
        ),
        rules=[
            OpenMetadataPolicyRule(
                name="DagsterBotRule-TestDefinition",
                description=(
                    "Read test definitions and create the one Dagster's checks "
                    "use when it is missing."
                ),
                effect="allow",
                resources=["testDefinition"],
                operations=["Create", "ViewBasic"],
            ),
            OpenMetadataPolicyRule(
                name="DagsterBotRule-TableTests",
                description=(
                    "Read and create test cases on tables and add their results."
                ),
                effect="allow",
                resources=["table"],
                operations=["CreateTests", "EditTests", "ViewTests"],
            ),
        ],
    ),
    # What ol-data-platform's `ol-dbt metrics sync` needs on top of
    # DefaultBotRole's ViewAll: PUT a metric (Create when it is new; a PUT to an
    # existing one is authorized as EditAll, EntityUtil.createOrUpdateOperation),
    # PATCH its description, owners, reviewers, tags, status and
    # extension.metricFileSha256 (covered by EditAll; /entityStatus maps to no
    # narrower operation), and PUT a lineage edge from each implemented_by
    # entity, which checks EditLineage on both ends (LineageResource.addLineage).
    # EditAll on metric covers the metric's end; the lineage rule lists every
    # entity type an implemented_by entry can name (ol-data-platform
    # ENTITY_COLLECTIONS).  DefaultBotPolicy's EditDisplayName deny does not
    # block the EditAll PUT, but EntityUpdater.updateDisplayName then keeps the
    # stored non-empty displayName for any bot it denies, so the sync can set a
    # metric's displayName only on create or where the metric has none.
    OpenMetadataPolicySpec(
        name="MetricsSyncBotPolicy",
        display_name="Metrics Sync Bot Policy",
        description=(
            "Lets ol-dbt metrics sync create and update Metric entities and add "
            "lineage to them from the entities that implement them."
        ),
        rules=[
            OpenMetadataPolicyRule(
                name="MetricsSyncBotRule-Metrics",
                description="Create metrics and edit any field of them.",
                effect="allow",
                resources=["metric"],
                operations=["Create", "EditAll"],
            ),
            OpenMetadataPolicyRule(
                name="MetricsSyncBotRule-Lineage",
                description=(
                    "Add lineage edges to metrics from the entities that "
                    "implement them."
                ),
                effect="allow",
                resources=[
                    "table",
                    "topic",
                    "apiEndpoint",
                    "dashboardDataModel",
                    "container",
                ],
                operations=["EditLineage"],
            ),
        ],
    ),
]

# These names are the values Keycloak sends in the `roles` claim, so renaming
# one here without renaming its Keycloak client role leaves its holders with no
# role.
ROLES: list[OpenMetadataRoleSpec] = [
    OpenMetadataRoleSpec(
        name="DataEngineer",
        display_name="Data Engineer",
        description=(
            "Maintains technical metadata, lineage and data quality tests. "
            "Granted by the ol-data-engineer and ol-platform-admin Keycloak "
            "roles."
        ),
        policies=["DataEngineerPolicy"],
    ),
    OpenMetadataRoleSpec(
        name="DataContributor",
        display_name="Data Contributor",
        description=(
            "Documents data assets and saves queries against them. Granted by "
            "the ol-researcher Keycloak role."
        ),
        policies=["DataContributorPolicy"],
    ),
    # Bots inherit no team policies, so these two, whose tokens live outside
    # OpenMetadata's own deployment, carry the deny themselves.
    OpenMetadataRoleSpec(
        name="DagsterBotRole",
        display_name="Dagster Bot Role",
        description="Role of dagster-bot.",
        policies=["DagsterBotPolicy", "HideAlertDestinationsPolicy"],
    ),
    OpenMetadataRoleSpec(
        name="MetricsSyncBotRole",
        display_name="Metrics Sync Bot Role",
        description="Role of metrics-sync-bot.",
        policies=["HideAlertDestinationsPolicy", "MetricsSyncBotPolicy"],
    ),
]

# Every user inherits the Organization team's policies.  OrganizationPolicy is
# the one the server seeds there (owner-based edit rights); it stays.
ORGANIZATION_POLICIES: list[str] = ["HideAlertDestinationsPolicy", "OrganizationPolicy"]

# Each is a Group (the only team type that can own assets and hold users) under
# Organization.  Asset ownership and alert routing refer to these names, so
# renaming one orphans what it owns.  Membership is assigned in the
# OpenMetadata UI.
#
# data-platform members are the stewards: DataSteward adds owners, display
# names, lineage and running ingestion to what DataConsumer allows.  The team is
# also who reviews data contracts, but in ol-data-platform pull requests:
# contracts are synced to OpenMetadata already Approved, so OpenMetadata's own
# approval step (an In Review contract that only its `reviewers` can approve) is
# not used.  The other teams' default roles are left as they are.
TEAMS: list[OpenMetadataTeamSpec] = [
    OpenMetadataTeamSpec(
        name="data-platform",
        display_name="Data Platform",
        description=(
            "Builds and operates the data platform. Default owner of warehouse assets."
        ),
        default_roles=["DataSteward"],
    ),
    OpenMetadataTeamSpec(
        name="analytics",
        display_name="Analytics",
        description="Analysts who work with the warehouse.",
    ),
    OpenMetadataTeamSpec(
        name="b2b",
        display_name="B2B",
        description=(
            "Reviews the B2B business metrics and partner dashboard definitions."
        ),
    ),
]
