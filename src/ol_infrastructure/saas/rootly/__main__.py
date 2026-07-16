"""Management of Rootly incident management resources."""

from pathlib import Path

import pulumi_rootly as rootly
from pulumi import Output, ResourceOptions

from bridge.secrets.sops import read_yaml_secrets

rootly_secrets = read_yaml_secrets(Path("rootly/account.yaml"))

# The bridged Rootly provider only reads the ROOTLY_API_TOKEN environment
# variable, and parameterized providers don't reliably pick up env vars, so
# the token is passed explicitly and every resource must set
# ResourceOptions(provider=rootly_provider).
rootly_provider = rootly.Provider(
    "rootly-provider",
    api_token=rootly_secrets["api_token"],
)
rootly_opts = ResourceOptions(provider=rootly_provider, protect=True)
# "secret" is ignored on all alert sources: Pulumi/provider secret comparison
# churn plans no-op updates even when the SOPS value matches the stored state.
rootly_alert_source_opts = ResourceOptions(
    provider=rootly_provider,
    protect=True,
    ignore_changes=["secret"],
)
# Pingdom's resolution rule is still provider-normalized into a no-op diff
# (present through provider v5.17.2), so it additionally ignores that field.
rootly_pingdom_alert_source_opts = ResourceOptions(
    provider=rootly_provider,
    protect=True,
    ignore_changes=["secret", "resolutionRuleAttributes"],
)

# CloudWatch alarms for QA/CI resources (e.g. "mitlearn-redis-qa-003") route
# through the same shared warning/critical SNS topics as production (see
# src/ol_infrastructure/lib/aws/monitoring_helper.py) and would otherwise page
# on-call just like a production incident. Interim mitigation until the
# underlying CloudWatch alarms stop sending actions for non-prod resources
# (see src/ol_infrastructure/components/aws/cache.py): demote urgency to
# Medium -- the same tier already used as the base urgency for the
# "Grafana Prometheus - QA" alert source -- for any alarm whose name contains
# a "-qa-" or "-ci-" environment segment.
CLOUDWATCH_NON_PROD_URGENCY_RULES = [
    {
        "alertUrgencyId": "fce5c971-6660-4ad9-90eb-e75122055f50",
        "jsonPath": "$.Message.AlarmName",
        "kind": "payload",
        "operator": "contains",
        "value": "-qa-",
    },
    {
        "alertUrgencyId": "fce5c971-6660-4ad9-90eb-e75122055f50",
        "jsonPath": "$.Message.AlarmName",
        "kind": "payload",
        "operator": "contains",
        "value": "-ci-",
    },
]

# Foundation resources imported from the existing Rootly account.
role_admin = rootly.Role(
    "admin",
    alerts_permissions=["create", "read"],
    api_keys_permissions=["create", "update", "read", "delete"],
    audits_permissions=["read"],
    catalogs_permissions=["create", "read", "update", "delete"],
    communication_permissions=["create", "read", "update", "delete"],
    edge_connector_permissions=["create", "read", "update", "delete"],
    environments_permissions=["create", "read", "update", "delete"],
    form_fields_permissions=["create", "read", "update", "delete"],
    functionalities_permissions=["create", "read", "update", "delete"],
    groups_permissions=["create", "read", "update", "delete"],
    incident_causes_permissions=["create", "read", "update", "delete"],
    incident_communication_permissions=["create", "read", "update", "delete", "send"],
    incident_feedbacks_permissions=["read", "create", "update"],
    incident_roles_permissions=["create", "read", "update", "delete"],
    incident_types_permissions=["create", "read", "update", "delete"],
    incidents_permissions=["create", "read", "update", "delete"],
    integrations_permissions=["create", "read", "update", "delete"],
    invitations_permissions=["create", "read", "update", "delete"],
    name="admin",
    paging_permissions=["create", "read", "update", "delete"],
    playbooks_permissions=["create", "read", "update", "delete"],
    private_incidents_permissions=["create", "read", "update", "delete"],
    pulses_permissions=["create", "update", "read"],
    retrospective_permissions=["create", "read", "update", "delete"],
    roles_permissions=["create", "read", "update", "delete"],
    secrets_permissions=["create", "read", "update", "delete"],
    services_permissions=["create", "read", "update", "delete"],
    severities_permissions=["create", "read", "update", "delete"],
    slas_permissions=["create", "read", "update", "delete"],
    status_pages_permissions=["create", "read", "update", "delete"],
    sub_statuses_permissions=["create", "read", "update", "delete"],
    webhooks_permissions=["create", "read", "update", "delete"],
    workflows_permissions=["create", "read", "update", "delete"],
    opts=rootly_opts,
)

role_none = rootly.Role(
    "none",
    name="None",
    opts=rootly_opts,
)

role_observer = rootly.Role(
    "observer",
    alerts_permissions=["create", "read"],
    api_keys_permissions=["read"],
    catalogs_permissions=["read"],
    communication_permissions=["read"],
    edge_connector_permissions=["read"],
    environments_permissions=["read"],
    form_fields_permissions=["read"],
    functionalities_permissions=["read"],
    groups_permissions=["read"],
    incident_causes_permissions=["read"],
    incident_communication_permissions=["read"],
    incident_feedbacks_permissions=["read"],
    incident_permission_set_id="b7fc1246-fe14-4a9d-84d0-c2e8b5b15e54",
    incident_roles_permissions=["read"],
    incident_types_permissions=["read"],
    incidents_permissions=["read", "create"],
    invitations_permissions=["read"],
    is_editable=True,
    name="observer",
    paging_permissions=["read"],
    playbooks_permissions=["read"],
    pulses_permissions=["create", "update", "read"],
    retrospective_permissions=["read"],
    services_permissions=["read"],
    severities_permissions=["read"],
    status_pages_permissions=["read"],
    sub_statuses_permissions=["read"],
    workflows_permissions=["read"],
    opts=rootly_opts,
)

role_owner = rootly.Role(
    "owner",
    alerts_permissions=["create", "read"],
    api_keys_permissions=["create", "update", "read", "delete"],
    audits_permissions=["read"],
    billing_permissions=["update"],
    catalogs_permissions=["create", "read", "update", "delete"],
    communication_permissions=["create", "read", "update", "delete"],
    edge_connector_permissions=["create", "read", "update", "delete"],
    environments_permissions=["create", "read", "update", "delete"],
    form_fields_permissions=["create", "read", "update", "delete"],
    functionalities_permissions=["create", "read", "update", "delete"],
    groups_permissions=["create", "read", "update", "delete"],
    incident_causes_permissions=["create", "read", "update", "delete"],
    incident_communication_permissions=["create", "read", "update", "delete", "send"],
    incident_feedbacks_permissions=["read", "create", "update"],
    incident_roles_permissions=["create", "read", "update", "delete"],
    incident_types_permissions=["create", "read", "update", "delete"],
    incidents_permissions=["create", "read", "update", "delete"],
    integrations_permissions=["create", "read", "update", "delete"],
    invitations_permissions=["create", "read", "update", "delete"],
    name="owner",
    paging_permissions=["create", "read", "update", "delete"],
    playbooks_permissions=["create", "read", "update", "delete"],
    private_incidents_permissions=["create", "read", "update", "delete"],
    pulses_permissions=["create", "update", "read"],
    retrospective_permissions=["create", "read", "update", "delete"],
    roles_permissions=["create", "read", "update", "delete"],
    secrets_permissions=["create", "read", "update", "delete"],
    services_permissions=["create", "read", "update", "delete"],
    severities_permissions=["create", "read", "update", "delete"],
    slas_permissions=["create", "read", "update", "delete"],
    status_pages_permissions=["create", "read", "update", "delete"],
    sub_statuses_permissions=["create", "read", "update", "delete"],
    webhooks_permissions=["create", "read", "update", "delete"],
    workflows_permissions=["create", "read", "update", "delete"],
    opts=rootly_opts,
)

role_user = rootly.Role(
    "user",
    alerts_permissions=["create", "read"],
    api_keys_permissions=["create", "update", "read", "delete"],
    catalogs_permissions=["read"],
    communication_permissions=["read"],
    edge_connector_permissions=["read"],
    environments_permissions=["create", "read", "update", "delete"],
    form_fields_permissions=["create", "read", "update", "delete"],
    functionalities_permissions=["create", "read", "update", "delete"],
    groups_permissions=["create", "read", "update", "delete"],
    incident_causes_permissions=["create", "read", "update", "delete"],
    incident_communication_permissions=["read"],
    incident_feedbacks_permissions=["read", "create", "update"],
    incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
    incident_roles_permissions=["create", "read", "update", "delete"],
    incident_types_permissions=["create", "read", "update", "delete"],
    incidents_permissions=["create", "read", "update", "delete"],
    invitations_permissions=["create", "read", "update", "delete"],
    is_editable=True,
    name="user",
    paging_permissions=["create", "read", "update", "delete"],
    playbooks_permissions=["create", "read", "update", "delete"],
    pulses_permissions=["create", "update", "read"],
    retrospective_permissions=["create", "read", "update", "delete"],
    secrets_permissions=["create", "read", "update", "delete"],
    services_permissions=["create", "read", "update", "delete"],
    severities_permissions=["create", "read", "update", "delete"],
    status_pages_permissions=["create", "read", "update", "delete"],
    sub_statuses_permissions=["read"],
    workflows_permissions=["create", "read", "update", "delete"],
    opts=rootly_opts,
)


team_platform_engineering = rootly.Team(
    "platform-engineering",
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    auto_add_members_when_attached=True,
    color="#F5D9C4",
    description="The go-to team for all incidents.",
    name="Platform Engineering",
    position=1,
    user_ids=[99415, 100683, 103372, 103392],
    opts=rootly_opts,
)

environment_development = rootly.Environment(
    "development",
    color="#D7E7F5",
    description="Development Environment",
    name="Development",
    position=1,
    opts=rootly_opts,
)

environment_production = rootly.Environment(
    "production",
    color="#F4CFD1",
    description="Production Environment",
    name="Production",
    position=3,
    opts=rootly_opts,
)

environment_staging = rootly.Environment(
    "staging",
    color="#FAEBB7",
    description="Staging Environment",
    name="Staging",
    position=2,
    opts=rootly_opts,
)

severity_sev0 = rootly.Severity(
    "sev0",
    color="#C73C40",
    description=(
        "Critical system issue actively impacting many customers' ability to "
        "use the product (e.g. website outage)"
    ),
    name="P1",
    position=1,
    severity="critical",
    opts=rootly_opts,
)

severity_sev1 = rootly.Severity(
    "sev1",
    color="#C73C40",
    description=(
        "Significant impact where major functionality is impacted and requires urgent "
        "attention (e.g. login broken)"
    ),
    name="P2",
    position=2,
    severity="high",
    opts=rootly_opts,
)

severity_sev2 = rootly.Severity(
    "sev2",
    color="#E58A1F",
    description=(
        "Moderate impact on specific features and requires prompt but not "
        "immediate attention (e.g. slow response times)"
    ),
    name="P3",
    position=3,
    severity="medium",
    opts=rootly_opts,
)


cause_bug = rootly.Cause(
    "bug",
    description="Bug in code",
    name="Bug",
    position=1,
    opts=rootly_opts,
)

cause_configuration_change = rootly.Cause(
    "configuration-change",
    description="Cause by change in configuration (eg. Set wrong environment variable)",
    name="Configuration Change",
    position=5,
    opts=rootly_opts,
)

cause_human_error = rootly.Cause(
    "human-error",
    description="Caused by human error",
    name="Human Error",
    position=3,
    opts=rootly_opts,
)

cause_load = rootly.Cause(
    "load",
    description="Excessive load or related issues on infrastructure",
    name="Load",
    position=2,
    opts=rootly_opts,
)

cause_r_3rd_party_outage = rootly.Cause(
    "r-3rd-party-outage",
    description="Errors caused by 3rd party service failure (eg. Stripe API Failing)",
    name="3rd Party Outage",
    position=4,
    opts=rootly_opts,
)

cause_unknown = rootly.Cause(
    "unknown",
    description="Exact cause can't be identified",
    name="Unknown",
    position=6,
    opts=rootly_opts,
)

incident_type_cloud = rootly.IncidentType(
    "cloud",
    color="#D7E7F5",
    description="Cloud provider related incidents (eg: AWS Kinesis outage)",
    name="Cloud",
    position=2,
    opts=rootly_opts,
)

incident_type_customer_facing = rootly.IncidentType(
    "customer-facing",
    color="#FAEBB7",
    description="Customers are actively experiencing a deteriorated experience",
    name="Customer Facing",
    position=3,
    opts=rootly_opts,
)

incident_type_default = rootly.IncidentType(
    "default",
    color="#D7F5E1",
    description="Used unless specific incident type is identified",
    name="Default",
    position=1,
    opts=rootly_opts,
)

incident_type_security = rootly.IncidentType(
    "security",
    color="#F4CFD1",
    description="Security related incidents (eg. PII leak, unauthorized access)",
    name="Security",
    position=4,
    opts=rootly_opts,
)

incident_role_commander = rootly.IncidentRole(
    "commander",
    description=(
        "You\u2019re the Incident Commander! You are the primary decision maker, "
        "responsible for driving the incident to resolution. This means:\n"
        " • Ensuring the right stakeholders are involved \n"
        "• Identifying tasks to move the incident forward \n"
        "• Keeping all responders informed on progress \n"
        "• Identifying and removing blockers"
    ),
    enabled=True,
    name="Commander",
    position=1,
    summary=(
        "Responsible for the overall management of the incident from start "
        "to finish, delegation of tasks across the response team, and final "
        "decision-making authority."
    ),
    opts=rootly_opts,
)

# On-call schedules and escalation resources imported from Rootly.
schedule_primary_on_call_schedule = rootly.Schedule(
    "primary-on-call-schedule",
    all_time_coverage=True,
    description="Platform Engineering Team On Call Schedule",
    name="Primary On Call Schedule",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    owner_user_id=99415,
    shift_report_day_of_week="monday",
    shift_report_enabled=True,
    shift_report_time_of_day="09:00",
    shift_report_time_zone="America/New_York",
    shift_start_notifications_enabled=True,
    shift_update_notifications_enabled=True,
    slack_channel={"id": "GBDLJJX51", "name": "devops-alerts"},
    slack_user_group={"id": "S9PK3B39V", "name": "sre"},
    opts=rootly_opts,
)

schedule_rotation_primary_rotation = rootly.ScheduleRotation(
    "primary-rotation",
    active_all_week=True,
    active_days=["M", "T", "W", "R", "F"],
    active_time_type="all_day",
    name="Primary Rotation",
    position=1,
    schedule_id="fad27d50-f0e4-4d21-9b6d-57eb2dec648b",
    schedule_rotation_members=[
        {"memberId": "99415", "memberType": "User", "position": 1},
        {"memberId": "103372", "memberType": "User", "position": 2},
        {"memberId": "100683", "memberType": "User", "position": 3},
        {"memberId": "103392", "memberType": "User", "position": 4},
    ],
    schedule_rotationable_attributes={"handoff_day": "T", "handoff_time": "10:00"},
    schedule_rotationable_type="ScheduleWeeklyRotation",
    time_zone="America/New_York",
    opts=rootly_opts,
)

escalation_policy_default_escalation_policy = rootly.EscalationPolicy(
    "default-escalation-policy",
    business_hours={
        "days": ["F", "M", "R", "T", "W"],
        "endTime": "17:00",
        "startTime": "09:00",
        "timeZone": "America/New_York",
    },
    created_by_user_id=99415,
    group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    last_updated_by_user_id=99415,
    name="Default Escalation Policy",
    repeat_count=5,
    service_ids=[
        "cdceaa06-6690-4351-a3af-dd36bfd6fb55",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
        "24ef3748-0a12-4a55-9b4e-5eb94a08fe03",
        "fa1c967f-d271-443f-b2bf-011cc78f5f20",
        "aaede19c-4521-40cd-ab64-6a2d70dd783c",
        "503028e7-d65f-44b6-8968-b9795ccc41c2",
        "2c92b8b0-df02-4369-a876-72a895524773",
        "ffd3bdea-a4f6-4f4a-a12a-f59e71f29fe9",
        "0d45ed4d-bd52-4488-9953-f739f18bbdaf",
        "3ad10823-4726-4207-9e99-0d81e87b0473",
        "b2389961-09be-4167-a304-a2ee1ef9af1b",
        "f42f1288-eca1-4bd4-b474-8a6bb96486fb",
        "8be387cb-2c05-4688-8ea0-730328297d62",
        "aa801b70-0496-4d9e-b889-e5ad99f2237b",
        "e7f7e16e-a7e7-4666-b779-96b33bbf402b",
        "5281c3c5-eb5e-4b7f-9407-950570d66261",
        "dc51f8d3-56fb-4ee3-b921-c9fdda79ea9c",
        "bdbf5e32-61dd-4184-9af6-f4c163e097d0",
        "6f9bf7d9-d06c-4ac4-8105-f8def1dc91d2",
        "e7b42ec2-216d-4e84-828e-97bd709b018b",
        "9fc1b049-d60c-4a4c-b5d9-95ea8db3aae8",
        "71bb3195-11a8-4a2d-b9b8-2b1dbbc5d6c2",
        "7b46658d-cd59-4c49-970b-9e9dc8998a7e",
        "0e8c091d-274d-4687-bb70-d85c5600e90b",
        "24abd4d9-4aac-4ea0-afc6-eb2106cc52fd",
        "ce09f262-1edf-475f-b145-3185d0da7241",
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "3fa021bd-25ba-4732-b588-a304cb1a104f",
        "aefc0e95-1376-41f2-b102-335a186e9eb7",
        "964321a2-ebd8-46d4-bd9e-c582cb4e4e49",
        "db3e4db5-fa3f-4239-a0f4-aa558847df66",
        "70173c97-f29d-453f-93ea-da9321d5984d",
        "defb1faa-e8a4-4fe5-8f03-4103667592f1",
        "dfc02e84-e281-43a6-b340-0e7cadd62036",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
        "2cc9bbde-ba8a-4c34-aa85-65f4d9c8aff4",
    ],
    opts=rootly_opts,
)

escalation_policy_exampledeleteme_escalationpolicy = rootly.EscalationPolicy(
    "exampledeleteme-escalationpolicy",
    business_hours={
        "days": ["F", "M", "R", "T", "W"],
        "endTime": "17:00",
        "startTime": "09:00",
        "timeZone": "America/New_York",
    },
    created_by_user_id=100683,
    description="Example escalation policy \u2013 safe to delete",
    last_updated_by_user_id=100683,
    name="exampleDeleteMe-EscalationPolicy",
    repeat_count=1,
    opts=rootly_opts,
)

escalation_level_b94aa0a3_cda6_4ee6_bcb1_cddf33c69088 = rootly.EscalationLevel(
    "b94aa0a3-cda6-4ee6-bcb1-cddf33c69088",
    delay=5,
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    escalation_policy_path_id="adc991bc-d498-4323-9d80-9d2dfa156b0c",
    notification_target_params=[
        {
            "id": "fad27d50-f0e4-4d21-9b6d-57eb2dec648b",
            "teamMembers": "all",
            "type": "schedule",
        }
    ],
    paging_strategy_configuration_schedule_strategy="on_call_only",
    paging_strategy_configuration_strategy="default",
    position=1,
    opts=rootly_opts,
)

escalation_level_r_4351b5b9_00d3_46ae_a044_05930cfbe0e2 = rootly.EscalationLevel(
    "r-4351b5b9-00d3-46ae-a044-05930cfbe0e2",
    delay=5,
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    escalation_policy_path_id="adc991bc-d498-4323-9d80-9d2dfa156b0c",
    notification_target_params=[{"id": "99415", "teamMembers": "all", "type": "user"}],
    paging_strategy_configuration_schedule_strategy="on_call_only",
    paging_strategy_configuration_strategy="default",
    position=3,
    opts=rootly_opts,
)

escalation_level_r_75bc919c_824c_46a1_9589_0fc8b85e0d77 = rootly.EscalationLevel(
    "r-75bc919c-824c-46a1-9589-0fc8b85e0d77",
    delay=5,
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    escalation_policy_path_id="67658f83-7fac-4a19-8e2a-0d8eee57f0a8",
    notification_target_params=[
        {
            "id": "fad27d50-f0e4-4d21-9b6d-57eb2dec648b",
            "teamMembers": "all",
            "type": "schedule",
        }
    ],
    paging_strategy_configuration_schedule_strategy="on_call_only",
    paging_strategy_configuration_strategy="default",
    position=1,
    opts=rootly_opts,
)

escalation_level_r_8ee197b2_ffe5_4696_b4a0_760e5c84a343 = rootly.EscalationLevel(
    "r-8ee197b2-ffe5-4696-b4a0-760e5c84a343",
    delay=10,
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    escalation_policy_path_id="adc991bc-d498-4323-9d80-9d2dfa156b0c",
    notification_target_params=[
        {
            "id": "fad27d50-f0e4-4d21-9b6d-57eb2dec648b",
            "teamMembers": "all",
            "type": "schedule",
        }
    ],
    paging_strategy_configuration_schedule_strategy="everyone",
    paging_strategy_configuration_strategy="default",
    position=2,
    opts=rootly_opts,
)

# Services imported from the existing Rootly account.
service_api_authentication = rootly.Service(
    "api-authentication",
    alerts_email_address="service-46529ebd894420307d1d29ba217f98c6@email.rootly.com",
    color="#FAEBB7",
    description="/api/v1/authentication",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="API - Authentication",
    position=1,
    public_description="Authentications API",
    opts=rootly_opts,
)

service_catchall = rootly.Service(
    "catchall",
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    alerts_email_address="service-2ff11b4f1f8db3ca31fd89b689d59e1e@email.rootly.com",
    color="#F4CFD1",
    description="A catch-all service for managing the bucket of on-call alerts",
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="CatchAll",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=2,
    opts=rootly_opts,
)

service_mit_learn_ai_celery = rootly.Service(
    "mit-learn-ai-celery",
    alerts_email_address="service-606c580534e94d6abeecf1c0ed65165f@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn AI - Celery",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=9,
    service_ids=["dfc02e84-e281-43a6-b340-0e7cadd62036"],
    opts=rootly_opts,
)

service_mit_learn_ai_django_webapp = rootly.Service(
    "mit-learn-ai-django-webapp",
    alerts_email_address="service-9aeba5fcc426230a11dcc5b8d39961b4@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn AI - Django - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=5,
    opts=rootly_opts,
)

service_mit_learn_ai_postgres = rootly.Service(
    "mit-learn-ai-postgres",
    alerts_email_address="service-772a22456d104f035fc94a5b4a746c1d@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn AI - Postgres",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=10,
    service_ids=[
        "964321a2-ebd8-46d4-bd9e-c582cb4e4e49",
        "dfc02e84-e281-43a6-b340-0e7cadd62036",
    ],
    opts=rootly_opts,
)

service_mit_learn_ai_redis = rootly.Service(
    "mit-learn-ai-redis",
    alerts_email_address="service-526b0e583f5aa081e180a59468bd1580@email.rootly.com",
    color="#F5D9C4",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn AI - Redis",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=11,
    service_ids=[
        "964321a2-ebd8-46d4-bd9e-c582cb4e4e49",
        "aefc0e95-1376-41f2-b102-335a186e9eb7",
        "dfc02e84-e281-43a6-b340-0e7cadd62036",
    ],
    opts=rootly_opts,
)

service_mit_learn_celery = rootly.Service(
    "mit-learn-celery",
    alerts_email_address="service-ccbf89b539067de95fdf152294530fbf@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Celery",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=7,
    service_ids=["c144023f-00c1-48dd-9e38-ad4c302207e3"],
    opts=rootly_opts,
)

service_mit_learn_django_webapp = rootly.Service(
    "mit-learn-django-webapp",
    alerts_email_address="service-fba68ba817b318d5191efbf049bca11e@email.rootly.com",
    color="#F4CFD1",
    description="The MIT Learn Django application",
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Django - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=3,
    opts=rootly_opts,
)

service_mit_learn_keycloak_postgres = rootly.Service(
    "mit-learn-keycloak-postgres",
    alerts_email_address="service-4b2f20be31d75a8c423be121b62d614d@email.rootly.com",
    color="#D7F5E1",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Keycloak - Postgres",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=28,
    service_ids=[
        "b2389961-09be-4167-a304-a2ee1ef9af1b",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
    ],
    opts=rootly_opts,
)

service_mit_learn_keycloak_webapp = rootly.Service(
    "mit-learn-keycloak-webapp",
    alerts_email_address="service-9ff6aa5af09428524710c20ee8d1cece@email.rootly.com",
    color="#D7E7F5",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Keycloak - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=27,
    service_ids=["c144023f-00c1-48dd-9e38-ad4c302207e3"],
    opts=rootly_opts,
)

service_mit_learn_nextjs = rootly.Service(
    "mit-learn-nextjs",
    alerts_email_address="service-46839f9ff9b65bd16a7ac5401b088fb7@email.rootly.com",
    color="#F5D9C4",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - NextJS",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=19,
    service_ids=[
        "70173c97-f29d-453f-93ea-da9321d5984d",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
    ],
    opts=rootly_opts,
)

service_mit_learn_opensearch = rootly.Service(
    "mit-learn-opensearch",
    alerts_email_address="service-3a22167842ef522229bfeb74564a50d6@email.rootly.com",
    color="#D7F5E1",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - OpenSearch",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=22,
    service_ids=[
        "0e8c091d-274d-4687-bb70-d85c5600e90b",
        "70173c97-f29d-453f-93ea-da9321d5984d",
        "7b46658d-cd59-4c49-970b-9e9dc8998a7e",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
    ],
    opts=rootly_opts,
)

service_mit_learn_postgres = rootly.Service(
    "mit-learn-postgres",
    alerts_email_address="service-f3766418147ec6095a387e2ea02ed7f2@email.rootly.com",
    color="#D7F5E1",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Postgres",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=21,
    service_ids=[
        "70173c97-f29d-453f-93ea-da9321d5984d",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
        "b2389961-09be-4167-a304-a2ee1ef9af1b",
        "0e8c091d-274d-4687-bb70-d85c5600e90b",
    ],
    opts=rootly_opts,
)

service_mit_learn_qdrant = rootly.Service(
    "mit-learn-qdrant",
    alerts_email_address="service-bd422ff284e0e642ee048b457f86747b@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Qdrant",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=23,
    service_ids=[
        "0e8c091d-274d-4687-bb70-d85c5600e90b",
        "7b46658d-cd59-4c49-970b-9e9dc8998a7e",
        "cdceaa06-6690-4351-a3af-dd36bfd6fb55",
        "b2389961-09be-4167-a304-a2ee1ef9af1b",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
        "70173c97-f29d-453f-93ea-da9321d5984d",
    ],
    opts=rootly_opts,
)

service_mit_learn_redis = rootly.Service(
    "mit-learn-redis",
    alerts_email_address="service-5d9b5ca0b9bbdd297fff043812cda66e@email.rootly.com",
    color="#D7F5E1",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Redis",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=20,
    service_ids=[
        "70173c97-f29d-453f-93ea-da9321d5984d",
        "b2389961-09be-4167-a304-a2ee1ef9af1b",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
    ],
    opts=rootly_opts,
)

service_mit_learn_tika = rootly.Service(
    "mit-learn-tika",
    alerts_email_address="service-5764d4c722af08470090742e05deaa01@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MIT Learn - Tika",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=24,
    service_ids=[
        "0e8c091d-274d-4687-bb70-d85c5600e90b",
        "71bb3195-11a8-4a2d-b9b8-2b1dbbc5d6c2",
        "7b46658d-cd59-4c49-970b-9e9dc8998a7e",
        "b2389961-09be-4167-a304-a2ee1ef9af1b",
        "cdceaa06-6690-4351-a3af-dd36bfd6fb55",
        "c144023f-00c1-48dd-9e38-ad4c302207e3",
        "70173c97-f29d-453f-93ea-da9321d5984d",
    ],
    opts=rootly_opts,
)

service_mitx_online_django_webapp = rootly.Service(
    "mitx-online-django-webapp",
    alerts_email_address="service-10a5bd157dae81df817ed17527fe2d36@email.rootly.com",
    color="#F4CFD1",
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Django - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=8,
    service_ids=[
        "db3e4db5-fa3f-4239-a0f4-aa558847df66",
        "ce09f262-1edf-475f-b145-3185d0da7241",
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
        "ffd3bdea-a4f6-4f4a-a12a-f59e71f29fe9",
        "24ef3748-0a12-4a55-9b4e-5eb94a08fe03",
        "24abd4d9-4aac-4ea0-afc6-eb2106cc52fd",
    ],
    opts=rootly_opts,
)

service_mitx_online_open_edx_cms_celery = rootly.Service(
    "mitx-online-open-edx-cms-celery",
    alerts_email_address="service-53ee1830012990a204a01724c4f81b5c@email.rootly.com",
    color="#F4CFD1",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - CMS - Celery",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=14,
    service_ids=[
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "db3e4db5-fa3f-4239-a0f4-aa558847df66",
    ],
    opts=rootly_opts,
)

service_mitx_online_open_edx_cms_webapp = rootly.Service(
    "mitx-online-open-edx-cms-webapp",
    alerts_email_address="service-0551387dc9834f25ebb34acab92b139d@email.rootly.com",
    color="#F5D9C4",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - CMS - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=13,
    service_ids=[
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
    ],
    opts=rootly_opts,
)

service_mitx_online_open_edx_lms_celery = rootly.Service(
    "mitx-online-open-edx-lms-celery",
    alerts_email_address="service-abdaa93edf00bf6e886c74e6747429d4@email.rootly.com",
    color="#FAEBB7",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - LMS - Celery",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=12,
    service_ids=["6ee39557-47af-40e9-a4f7-eccee9406ecf"],
    opts=rootly_opts,
)

service_mitx_online_open_edx_lms_webapp = rootly.Service(
    "mitx-online-open-edx-lms-webapp",
    alerts_email_address="service-f086a5a4ac28330542b82a8ad7f918a8@email.rootly.com",
    color="#F5D9C4",
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - LMS - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=4,
    opts=rootly_opts,
)

service_mitx_online_open_edx_mongodb = rootly.Service(
    "mitx-online-open-edx-mongodb",
    alerts_email_address="service-6d7fba2ed41bb7047b8b1bf89daf76be@email.rootly.com",
    color="#D7E7F5",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - MongoDB",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=16,
    service_ids=[
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
        "ce09f262-1edf-475f-b145-3185d0da7241",
    ],
    opts=rootly_opts,
)

service_mitx_online_open_edx_mysql = rootly.Service(
    "mitx-online-open-edx-mysql",
    alerts_email_address="service-a012b7c2355686c237dc928922ebec65@email.rootly.com",
    color="#F5D9C4",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - MySQL",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=15,
    service_ids=[
        "ce09f262-1edf-475f-b145-3185d0da7241",
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
    ],
    opts=rootly_opts,
)

service_mitx_online_open_edx_opensearch = rootly.Service(
    "mitx-online-open-edx-opensearch",
    alerts_email_address="service-a31f2a70d7ddde0f01ead0513886d960@email.rootly.com",
    color="#F4CFD1",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - OpenSearch",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=18,
    service_ids=[
        "db3e4db5-fa3f-4239-a0f4-aa558847df66",
        "ce09f262-1edf-475f-b145-3185d0da7241",
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
        "ffd3bdea-a4f6-4f4a-a12a-f59e71f29fe9",
        "24ef3748-0a12-4a55-9b4e-5eb94a08fe03",
        "24abd4d9-4aac-4ea0-afc6-eb2106cc52fd",
    ],
    opts=rootly_opts,
)

service_mitx_online_open_edx_redis = rootly.Service(
    "mitx-online-open-edx-redis",
    alerts_email_address="service-7fed2569a32ff298e909307fcc3924d6@email.rootly.com",
    color="#F4CFD1",
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="MITx Online - Open edX - Redis",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=17,
    service_ids=[
        "ce09f262-1edf-475f-b145-3185d0da7241",
        "0f046ecc-a5eb-4cdf-aba2-6922001ad774",
        "145db75f-c893-444b-9564-85ff41d42c6a",
        "6ee39557-47af-40e9-a4f7-eccee9406ecf",
        "db3e4db5-fa3f-4239-a0f4-aa558847df66",
    ],
    opts=rootly_opts,
)

service_odl_video_celery = rootly.Service(
    "odl-video-celery",
    alerts_email_address="service-471e6eae485ad11c9f8694514de6a705@email.rootly.com",
    color="#F5D9C4",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="ODL Video - Celery",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=25,
    service_ids=["defb1faa-e8a4-4fe5-8f03-4103667592f1"],
    opts=rootly_opts,
)

service_odl_video_django_webapp = rootly.Service(
    "odl-video-django-webapp",
    alerts_email_address="service-82522ca1f511fd7ee7c6a374277bc515@email.rootly.com",
    color="#D7E7F5",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    escalation_policy_id="96629210-cc41-4e57-b059-b182a0f01c5b",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="ODL Video - Django - Webapp",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=6,
    opts=rootly_opts,
)

service_odl_video_postgres = rootly.Service(
    "odl-video-postgres",
    alerts_email_address="service-882cdae62ccfa4770890b84e053f85c7@email.rootly.com",
    color="#F5D9C4",
    environment_ids=["afe3e34e-62e7-4534-bb4c-de57d24e6a59"],
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="ODL Video - Postgres",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    position=26,
    service_ids=[
        "9fc1b049-d60c-4a4c-b5d9-95ea8db3aae8",
        "defb1faa-e8a4-4fe5-8f03-4103667592f1",
    ],
    opts=rootly_opts,
)

service_ui_user_profile_block = rootly.Service(
    "ui-user-profile-block",
    alerts_email_address="service-8b603ff24154ed2fb90e11bd19982ad2@email.rootly.com",
    color="#D7E7F5",
    description="Displays user details.",
    github_repository_branch="master",
    gitlab_repository_branch="master",
    name="UI - User Profile Block",
    position=1,
    public_description="User Profile UI Block",
    opts=rootly_opts,
)

# Dashboards and panels imported from Rootly.
dashboard_on_call_metrics = rootly.Dashboard(
    "on-call-metrics",
    name="On-Call Metrics",
    owner="team",
    opts=rootly_opts,
)

dashboard_overview = rootly.Dashboard(
    "overview",
    name="Overview",
    owner="team",
    opts=rootly_opts,
)

dashboard_workload = rootly.Dashboard(
    "workload",
    name="Workload",
    owner="team",
    opts=rootly_opts,
)

dashboard_panel_acknowledge_rate = rootly.DashboardPanel(
    "acknowledge-rate",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Acknowledge Rate",
    params={
        "datasets": [
            {
                "aggregate": {"key": "acknowledge_rate", "operation": "average"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "name": "Acknowledge Rate",
            }
        ],
        "description": "Percentage of alerts acknowledged",
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 5},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_escalation_policy = rootly.DashboardPanel(
    "alerts-by-escalation-policy",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Escalation Policy",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "escalation_policies",
            }
        ],
        "display": "pie_chart",
        "legend": {"groups": "charted"},
    },
    position={"h": 3, "w": 6, "x": 6, "y": 14},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_responder = rootly.DashboardPanel(
    "alerts-by-responder",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Responder",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "responders",
            }
        ],
        "display": "pie_chart",
        "legend": {"groups": "charted"},
    },
    position={"h": 3, "w": 6, "x": 0, "y": 11},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_service = rootly.DashboardPanel(
    "alerts-by-service",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Service",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "services",
            }
        ],
        "display": "line_chart",
        "legend": {"groups": "charted"},
    },
    position={"h": 3, "w": 6, "x": 0, "y": 17},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_service_1 = rootly.DashboardPanel(
    "alerts-by-service-1",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Service",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "services",
            }
        ],
        "display": "pie_chart",
        "legend": {"groups": "charted"},
    },
    position={"h": 3, "w": 6, "x": 6, "y": 17},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_source = rootly.DashboardPanel(
    "alerts-by-source",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Source",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "source",
                "name": "Alerts",
            }
        ],
        "display": "line_chart",
        "legend": {"groups": "all"},
    },
    position={"h": 3, "w": 6, "x": 0, "y": 8},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_source_1 = rootly.DashboardPanel(
    "alerts-by-source-1",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Source",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "source",
                "name": "Alerts",
            }
        ],
        "display": "pie_chart",
        "legend": {"groups": "all"},
    },
    position={"h": 3, "w": 6, "x": 6, "y": 8},
    opts=rootly_opts,
)

dashboard_panel_alerts_by_urgency = rootly.DashboardPanel(
    "alerts-by-urgency",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Alerts by Urgency",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "alert_urgency",
                "name": "Alerts",
            }
        ],
        "display": "line_chart",
        "legend": {"groups": "all"},
    },
    position={"h": 3, "w": 6, "x": 0, "y": 14},
    opts=rootly_opts,
)

dashboard_panel_hours_worked_by_incident_using_resolution_time = rootly.DashboardPanel(
    "hours-worked-by-incident-using-resolution-time",
    dashboard_id="67d27f53-e6f4-4720-8575-ae60f78bbb8f",
    name="Hours Worked by Incident (Using resolution time)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "hours_worked_until_resolved", "operation": "sum"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "table",
    },
    position={"h": 6, "w": 6, "x": 0, "y": 4},
    opts=rootly_opts,
)

dashboard_panel_hours_worked_by_user_using_resolution_time = rootly.DashboardPanel(
    "hours-worked-by-user-using-resolution-time",
    dashboard_id="67d27f53-e6f4-4720-8575-ae60f78bbb8f",
    name="Hours Worked by User (Using resolution time)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "hours_worked_until_resolved", "operation": "sum"},
                "collection": "users",
            }
        ],
        "display": "table",
    },
    position={"h": 6, "w": 6, "x": 6, "y": 4},
    opts=rootly_opts,
)

dashboard_panel_hours_worked_using_resolution_time = rootly.DashboardPanel(
    "hours-worked-using-resolution-time",
    dashboard_id="67d27f53-e6f4-4720-8575-ae60f78bbb8f",
    name="Hours Worked (Using resolution time)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "hours_worked_until_resolved", "operation": "sum"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "column_chart",
    },
    position={"h": 4, "w": 12, "x": 0, "y": 0},
    opts=rootly_opts,
)

dashboard_panel_incident_retrospectives_cause = rootly.DashboardPanel(
    "incident-retrospectives-cause",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incident Retrospectives/Cause",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incident_post_mortems",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "causes",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 18},
    opts=rootly_opts,
)

dashboard_panel_incident_retrospectives_cause_1 = rootly.DashboardPanel(
    "incident-retrospectives-cause-1",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incident Retrospectives/Cause",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incident_post_mortems",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "causes",
            }
        ],
        "display": "pie_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 18},
    opts=rootly_opts,
)

dashboard_panel_incidents_environment = rootly.DashboardPanel(
    "incidents-environment",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Environment",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "environments",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 6},
    opts=rootly_opts,
)

dashboard_panel_incidents_environment_1 = rootly.DashboardPanel(
    "incidents-environment-1",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Environment",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "environments",
            }
        ],
        "display": "pie_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 6},
    opts=rootly_opts,
)

dashboard_panel_incidents_functionality = rootly.DashboardPanel(
    "incidents-functionality",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Functionality",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "functionalities",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 12},
    opts=rootly_opts,
)

dashboard_panel_incidents_functionality_1 = rootly.DashboardPanel(
    "incidents-functionality-1",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Functionality",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "functionalities",
            }
        ],
        "display": "pie_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 12},
    opts=rootly_opts,
)

dashboard_panel_incidents_service = rootly.DashboardPanel(
    "incidents-service",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Service",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "services",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 9},
    opts=rootly_opts,
)

dashboard_panel_incidents_service_1 = rootly.DashboardPanel(
    "incidents-service-1",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Service",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "services",
            }
        ],
        "display": "pie_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 9},
    opts=rootly_opts,
)

dashboard_panel_incidents_severity = rootly.DashboardPanel(
    "incidents-severity",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Severity",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "severity",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 3},
    opts=rootly_opts,
)

dashboard_panel_incidents_severity_1 = rootly.DashboardPanel(
    "incidents-severity-1",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Severity",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "severity",
            }
        ],
        "display": "pie_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 3},
    opts=rootly_opts,
)

dashboard_panel_incidents_type = rootly.DashboardPanel(
    "incidents-type",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Type",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "incident_types",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 15},
    opts=rootly_opts,
)

dashboard_panel_incidents_type_1 = rootly.DashboardPanel(
    "incidents-type-1",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="Incidents/Type",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
                "groupBy": "incident_types",
            }
        ],
        "display": "pie_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 15},
    opts=rootly_opts,
)

dashboard_panel_mean_time_between_failure = rootly.DashboardPanel(
    "mean-time-between-failure",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Mean Time between Failure",
    params={
        "datasets": [
            {
                "aggregate": {"key": "time_between_failure", "operation": "average"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
            }
        ],
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 5},
    opts=rootly_opts,
)

dashboard_panel_mean_time_to_acknowledge = rootly.DashboardPanel(
    "mean-time-to-acknowledge",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Mean Time to Acknowledge",
    params={
        "datasets": [
            {
                "aggregate": {"key": "acknowledge_time", "operation": "average"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
            }
        ],
        "display": "line_chart",
    },
    position={"h": 2, "w": 4, "x": 4, "y": 0},
    opts=rootly_opts,
)

dashboard_panel_mean_time_to_resolve = rootly.DashboardPanel(
    "mean-time-to-resolve",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Mean Time to Resolve",
    params={
        "datasets": [
            {
                "aggregate": {"key": "resolution_time", "operation": "average"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "name": "MTTR",
            }
        ],
        "display": "line_chart",
    },
    position={"h": 2, "w": 4, "x": 8, "y": 0},
    opts=rootly_opts,
)

dashboard_panel_mtta_by_responder = rootly.DashboardPanel(
    "mtta-by-responder",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="MTTA by Responder",
    params={
        "datasets": [
            {
                "aggregate": {"key": "acknowledge_time", "operation": "average"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "responders",
            }
        ],
        "display": "line_chart",
        "legend": {"groups": "charted"},
    },
    position={"h": 3, "w": 6, "x": 0, "y": 2},
    opts=rootly_opts,
)

dashboard_panel_mtta_mean_time_to_acknowledge = rootly.DashboardPanel(
    "mtta-mean-time-to-acknowledge",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="MTTA (Mean Time To Acknowledge)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "acknowledge_time", "operation": "average"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 2, "w": 3, "x": 3, "y": 1},
    opts=rootly_opts,
)

dashboard_panel_mttd_mean_time_to_detection = rootly.DashboardPanel(
    "mttd-mean-time-to-detection",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="MTTD (Mean Time To Detection)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "detection_time", "operation": "average"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 2, "w": 3, "x": 0, "y": 1},
    opts=rootly_opts,
)

dashboard_panel_mttm_mean_time_to_mitigation = rootly.DashboardPanel(
    "mttm-mean-time-to-mitigation",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="MTTM (Mean Time To Mitigation)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "mitigation_time", "operation": "average"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 2, "w": 3, "x": 6, "y": 1},
    opts=rootly_opts,
)

dashboard_panel_mttr_by_service = rootly.DashboardPanel(
    "mttr-by-service",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="MTTR by Service",
    params={
        "datasets": [
            {
                "aggregate": {"key": "resolution_time", "operation": "average"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "services",
            }
        ],
        "display": "line_chart",
        "legend": {"groups": "charted"},
    },
    position={"h": 3, "w": 6, "x": 6, "y": 2},
    opts=rootly_opts,
)

dashboard_panel_mttr_mean_time_to_resolution = rootly.DashboardPanel(
    "mttr-mean-time-to-resolution",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="MTTR (Mean Time To Resolution)",
    params={
        "datasets": [
            {
                "aggregate": {"key": "resolution_time", "operation": "average"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 2, "w": 3, "x": 9, "y": 1},
    opts=rootly_opts,
)

dashboard_panel_of_action_items = rootly.DashboardPanel(
    "of-action-items",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="# of Action Items",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incident_action_items",
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 1, "w": 4, "x": 8, "y": 0},
    opts=rootly_opts,
)

dashboard_panel_of_incidents = rootly.DashboardPanel(
    "of-incidents",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="# of Incidents",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incidents",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 1, "w": 4, "x": 0, "y": 0},
    opts=rootly_opts,
)

dashboard_panel_of_retrospectives = rootly.DashboardPanel(
    "of-retrospectives",
    dashboard_id="cfc059e2-f735-4527-bc9b-d1a2661c0870",
    name="# of Retrospectives",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "incident_post_mortems",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "and",
                                "value": "normal",
                            },
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "or",
                                "value": "normal_sub",
                            },
                            {
                                "condition": "=",
                                "key": "incidents.kind",
                                "operation": "or",
                                "value": "backfilled",
                            },
                        ],
                    }
                ],
            }
        ],
        "display": "aggregate_value",
    },
    position={"h": 1, "w": 4, "x": 4, "y": 0},
    opts=rootly_opts,
)

dashboard_panel_response_effort = rootly.DashboardPanel(
    "response-effort",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Response Effort",
    params={
        "datasets": [
            {
                "aggregate": {"key": "resolve_time", "operation": "sum"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [
                            {
                                "condition": "=",
                                "key": "status",
                                "operation": "and",
                                "value": "resolved",
                            }
                        ],
                    }
                ],
                "name": "Response Effort",
            }
        ],
        "description": "Sum of time between acknowledge and resolve.",
        "display": "line_chart",
    },
    position={"h": 3, "w": 6, "x": 6, "y": 11},
    opts=rootly_opts,
)

dashboard_panel_signal_noise_ratio = rootly.DashboardPanel(
    "signal-noise-ratio",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Signal:Noise Ratio",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
                "groupBy": "noise",
            }
        ],
        "description": "Percentage of Alerts marked as noisy",
        "display": "stacked_column_chart",
    },
    position={"h": 3, "w": 6, "x": 0, "y": 20},
    opts=rootly_opts,
)

dashboard_panel_total_alerts = rootly.DashboardPanel(
    "total-alerts",
    dashboard_id="e09aa511-706e-44ad-8210-1faf028cf852",
    name="Total Alerts",
    params={
        "datasets": [
            {
                "aggregate": {"key": "results", "operation": "count"},
                "collection": "alerts",
                "filters": [
                    {
                        "operation": "and",
                        "rules": [{"condition": "=", "operation": "and"}],
                    }
                ],
            }
        ],
        "display": "line_chart",
    },
    position={"h": 2, "w": 4, "x": 0, "y": 0},
    opts=rootly_opts,
)

# Incident permission sets imported from Rootly.
incident_permission_set_default = rootly.IncidentPermissionSet(
    "default",
    name="Default",
    private_incident_permissions=["create", "read", "update", "delete"],
    public_incident_permissions=["create", "read", "update", "delete"],
    opts=rootly_opts,
)

incident_permission_set_observer = rootly.IncidentPermissionSet(
    "observer",
    name="Observer",
    private_incident_permissions=["read"],
    public_incident_permissions=["read", "create"],
    opts=rootly_opts,
)

incident_permission_set_resource_afdf01e7_26ef_4cae_a087_84d212256291 = (
    rootly.IncidentPermissionSetResource(
        "afdf01e7-26ef-4cae-a087-84d212256291",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        private=True,
        resource_id="eeba07e9-98b6-4bbe-a866-012f6fc54a10",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_b64b85b2_6e6f_4617_8012_afb3eb91f8fe = (
    rootly.IncidentPermissionSetResource(
        "b64b85b2-6e6f-4617-8012-afb3eb91f8fe",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        resource_id="88ca4512-75c5-4412-8382-ef27f2173965",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_c36bc1d5_f81b_46a5_9ef1_49526fc77afe = (
    rootly.IncidentPermissionSetResource(
        "c36bc1d5-f81b-46a5-9ef1-49526fc77afe",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="sub_statuses",
        private=True,
        resource_id="87ffaafa-f750-4665-acb2-7282bc928bd2",
        resource_type="SubStatus",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_cc047142_e1a7_4209_b6c1_b0593e4b4e0e = (
    rootly.IncidentPermissionSetResource(
        "cc047142-e1a7-4209-b6c1-b0593e4b4e0e",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        resource_id="53c5f099-4bbe-48e2-b2ac-e609ab432ca3",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_cc28a686_47ca_4558_a75c_415eba832c2b = (
    rootly.IncidentPermissionSetResource(
        "cc28a686-47ca-4558-a75c-415eba832c2b",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        private=True,
        resource_id="3475fc87-b702-4808-9484-abf6f3a667c4",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_e0de8394_a8ab_4f92_9cfb_b14820450221 = (
    rootly.IncidentPermissionSetResource(
        "e0de8394-a8ab-4f92-9cfb-b14820450221",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        private=True,
        resource_id="f06a2a05-863b-4463-903d-aa3f1df925b3",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_e7deb99d_c2aa_445c_8e51_3d0c3238ed2b = (
    rootly.IncidentPermissionSetResource(
        "e7deb99d-c2aa-445c-8e51-3d0c3238ed2b",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        resource_id="3475fc87-b702-4808-9484-abf6f3a667c4",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_ef305fd4_a1b1_4c72_9a5b_9e8c7240088c = (
    rootly.IncidentPermissionSetResource(
        "ef305fd4-a1b1-4c72-9a5b-9e8c7240088c",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        private=True,
        resource_id="88ca4512-75c5-4412-8382-ef27f2173965",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_f387a7d4_51b0_4c27_a192_f10ed3deed9b = (
    rootly.IncidentPermissionSetResource(
        "f387a7d4-51b0-4c27-a192-f10ed3deed9b",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        resource_id="7e6e22f4-01d5-4cb0-9559-cf59175bac99",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_f63876c7_40b6_41fd_a1e8_6bbae069d393 = (
    rootly.IncidentPermissionSetResource(
        "f63876c7-40b6-41fd-a1e8-6bbae069d393",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        resource_id="0441a067-a6ca-449b-9a66-2210c125891c",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_f8a9b4ea_e2de_4141_bfb4_e85dd771854b = (
    rootly.IncidentPermissionSetResource(
        "f8a9b4ea-e2de-4141-bfb4-e85dd771854b",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        private=True,
        resource_id="7b8a3624-510d-4b02-8db4-a737d033b589",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_fd4fe72a_077c_4a48_a991_7cc2f0820e6a = (
    rootly.IncidentPermissionSetResource(
        "fd4fe72a-077c-4a48-a991-7cc2f0820e6a",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        private=True,
        resource_id="0b70d00c-d079-42ee-ae16-d81bffcb8277",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_fefcddc6_33b3_4b44_9dc2_143b1a514527 = (
    rootly.IncidentPermissionSetResource(
        "fefcddc6-33b3-4b44-9dc2-143b1a514527",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        private=True,
        resource_id="0441a067-a6ca-449b-9a66-2210c125891c",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_00fc317d_bcad_487c_89f1_59c2a7c76f3f = (
    rootly.IncidentPermissionSetResource(
        "r-00fc317d-bcad-487c-89f1-59c2a7c76f3f",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        private=True,
        resource_id="d5f0e34d-64af-4b18-86a1-1980b7eef645",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_2e3ba214_b247_4ec0_862f_51cf40e3eed9 = (
    rootly.IncidentPermissionSetResource(
        "r-2e3ba214-b247-4ec0-862f-51cf40e3eed9",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="sub_statuses",
        private=True,
        resource_id="5beef0a2-a476-478d-9fb4-36d946dcc9a1",
        resource_type="SubStatus",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_39a8dc3e_0261_4a74_b2c1_9c03955cc1b9 = (
    rootly.IncidentPermissionSetResource(
        "r-39a8dc3e-0261-4a74-b2c1-9c03955cc1b9",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="sub_statuses",
        resource_id="87ffaafa-f750-4665-acb2-7282bc928bd2",
        resource_type="SubStatus",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_43e916e1_e447_433c_9f54_b5c312b48208 = (
    rootly.IncidentPermissionSetResource(
        "r-43e916e1-e447-433c-9f54-b5c312b48208",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="sub_statuses",
        resource_id="5beef0a2-a476-478d-9fb4-36d946dcc9a1",
        resource_type="SubStatus",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_451d158b_ea57_49ec_8bba_6ff2b9b9d2a0 = (
    rootly.IncidentPermissionSetResource(
        "r-451d158b-ea57-49ec-8bba-6ff2b9b9d2a0",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        resource_id="d5f0e34d-64af-4b18-86a1-1980b7eef645",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_4c11aad7_e06e_4026_9ba6_c8a7967ebe16 = (
    rootly.IncidentPermissionSetResource(
        "r-4c11aad7-e06e-4026-9ba6-c8a7967ebe16",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        resource_id="eeba07e9-98b6-4bbe-a866-012f6fc54a10",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_4f5dd2ac_5b61_45fa_8258_8c44275797a2 = (
    rootly.IncidentPermissionSetResource(
        "r-4f5dd2ac-5b61-45fa-8258-8c44275797a2",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        private=True,
        resource_id="7e6e22f4-01d5-4cb0-9559-cf59175bac99",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_53150946_143c_4437_aabe_a6d92a56b97c = (
    rootly.IncidentPermissionSetResource(
        "r-53150946-143c-4437-aabe-a6d92a56b97c",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="severities",
        resource_id="f06a2a05-863b-4463-903d-aa3f1df925b3",
        resource_type="Severity",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_61e70b52_9657_40c6_afb4_25273cccdc69 = (
    rootly.IncidentPermissionSetResource(
        "r-61e70b52-9657-40c6-afb4-25273cccdc69",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        resource_id="7b8a3624-510d-4b02-8db4-a737d033b589",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_631f215f_ffb6_426a_9552_cad42bed759b = (
    rootly.IncidentPermissionSetResource(
        "r-631f215f-ffb6-426a-9552-cad42bed759b",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        resource_id="0b70d00c-d079-42ee-ae16-d81bffcb8277",
        resource_type="Status",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_69c58cb0_32ea_4232_810d_0d09aae2cbc6 = (
    rootly.IncidentPermissionSetResource(
        "r-69c58cb0-32ea-4232-810d-0d09aae2cbc6",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="sub_statuses",
        resource_id="4d8dbfd1-7c8d-4a16-a7e8-4b9f609a87cc",
        resource_type="SubStatus",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_6a5aed55_1e51_4fbf_a7b7_dfa89095641c = (
    rootly.IncidentPermissionSetResource(
        "r-6a5aed55-1e51-4fbf-a7b7-dfa89095641c",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="sub_statuses",
        private=True,
        resource_id="4d8dbfd1-7c8d-4a16-a7e8-4b9f609a87cc",
        resource_type="SubStatus",
        opts=rootly_opts,
    )
)

incident_permission_set_resource_r_71be0d05_7cae_48af_a2d6_f8eb1dae6570 = (
    rootly.IncidentPermissionSetResource(
        "r-71be0d05-7cae-48af-a2d6-f8eb1dae6570",
        incident_permission_set_id="3bc47de8-5079-4110-8e47-c7ff58f109d9",
        kind="statuses",
        private=True,
        resource_id="53c5f099-4bbe-48e2-b2ac-e609ab432ca3",
        resource_type="Status",
        opts=rootly_opts,
    )
)

sev3 = rootly.Severity(
    "sev3",
    color="#7748F6",
    description=(
        "A minor inconvenience to customers with a workaround available "
        "(e.g. display bug)"
    ),
    name="P4",
    position=4,
    severity="low",
    opts=rootly_opts,
)

# Alert sources and routes imported from Rootly.
alerts_source_cloudwatch_critical = rootly.AlertsSource(
    "cloudwatch-critical",
    alert_source_fields_attributes=[
        {"alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834"},
        {"alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6"},
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_source_urgency_rules_attributes=CLOUDWATCH_NON_PROD_URGENCY_RULES,
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    name="Cloudwatch - Critical",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    secret=Output.secret(rootly_secrets["alert_source_secrets"]["cloudwatch_critical"]),
    source_type="cloud_watch",
    status="setup_incomplete",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/cloud_watch_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_cloudwatch_warning = rootly.AlertsSource(
    "cloudwatch-warning",
    alert_source_fields_attributes=[
        {"alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834"},
        {"alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6"},
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_source_urgency_rules_attributes=CLOUDWATCH_NON_PROD_URGENCY_RULES,
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    name="Cloudwatch - Warning",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    secret=Output.secret(rootly_secrets["alert_source_secrets"]["cloudwatch_warning"]),
    source_type="cloud_watch",
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/cloud_watch_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_grafana = rootly.AlertsSource(
    "grafana",
    alert_source_fields_attributes=[
        {
            "alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834",
            "templateBody": "{{ alert.data.title }}",
        },
        {
            "alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6",
            "templateBody": "{{ alert.description }}",
        },
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    name="Grafana",
    secret=Output.secret(rootly_secrets["alert_source_secrets"]["grafana"]),
    source_type="grafana",
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/grafana_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_grafana_prometheus_ci = rootly.AlertsSource(
    "grafana-prometheus-ci",
    alert_source_fields_attributes=[
        {"alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834"},
        {"alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6"},
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    name="Grafana Prometheus - CI",
    secret=Output.secret(
        rootly_secrets["alert_source_secrets"]["grafana_prometheus_ci"]
    ),
    source_type="alertmanager",
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/alertmanager_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_grafana_prometheus_production = rootly.AlertsSource(
    "grafana-prometheus-production",
    alert_source_fields_attributes=[
        {"alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834"},
        {"alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6"},
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_source_urgency_rules_attributes=[
        {
            "alertUrgencyId": "d7ed8e91-ffa9-4cc4-b524-729d14a4425b",
            "jsonPath": "$.commonLabels.severity",
            "kind": "payload",
            "operator": "is",
            "value": "warning",
        },
        {
            "alertUrgencyId": "fce5c971-6660-4ad9-90eb-e75122055f50",
            "conditionableId": "39b50c54-efa8-47fc-acc6-90f1455a8834",
            "conditionableType": "AlertField",
            "kind": "alert_field",
            "operator": "is",
            "value": "DiskUsageCritical",
        },
    ],
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    name="Grafana Prometheus - Production",
    secret=Output.secret(
        rootly_secrets["alert_source_secrets"]["grafana_prometheus_production"]
    ),
    source_type="alertmanager",
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/alertmanager_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_grafana_prometheus_qa = rootly.AlertsSource(
    "grafana-prometheus-qa",
    alert_source_fields_attributes=[
        {"alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834"},
        {"alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6"},
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_urgency_id="fce5c971-6660-4ad9-90eb-e75122055f50",
    deduplication_key_kind="payload",
    name="Grafana Prometheus - QA",
    secret=Output.secret(
        rootly_secrets["alert_source_secrets"]["grafana_prometheus_qa"]
    ),
    source_type="alertmanager",
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/alertmanager_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_mitol_sentry = rootly.AlertsSource(
    "mitol-sentry",
    alert_source_fields_attributes=[
        {
            "alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6",
            "templateBody": "An alert has been triggered from {{ "
            "alert.data.data.metric_alert.projects[0] }}:\n"
            "{{ alert.data.data.description_text }}",
        },
        {
            "alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc",
            "templateBody": "{{  alert.data.data.web_url }}",
        },
        {
            "alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834",
            "templateBody": "{{ alert.data.data.issue.title }}",
        },
    ],
    alert_source_urgency_rules_attributes=[
        {
            "alertUrgencyId": "d7ed8e91-ffa9-4cc4-b524-729d14a4425b",
            "jsonPath": "$.data.action",
            "kind": "payload",
            "operator": "is_not",
            "value": "critical",
        }
    ],
    alert_urgency_id="d7ed8e91-ffa9-4cc4-b524-729d14a4425b",
    deduplication_key_kind="payload",
    name="MITOL Sentry",
    secret=Output.secret(rootly_secrets["alert_source_secrets"]["mitol_sentry"]),
    source_type="sentry",
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/sentry_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_alert_source_opts,
)

alerts_source_pingdom = rootly.AlertsSource(
    "pingdom",
    alert_source_fields_attributes=[
        {
            "alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6",
            "templateBody": "Long Description: {{ alert.data.long_description }}\n"
            "\n"
            "Full URL: {{ alert.data.check_params.full_url }}\n",
        },
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
        {
            "alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834",
            "templateBody": "{{ alert.data.check_name }}: {{ alert.data.description }}",
        },
    ],
    alert_source_urgency_rules_attributes=[
        {
            "alertUrgencyId": "d7ed8e91-ffa9-4cc4-b524-729d14a4425b",
            "jsonPath": "$.importance_level",
            "kind": "payload",
            "operator": "is",
            "value": "LOW",
        }
    ],
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    deduplication_key_path="$.check_id",
    name="Pingdom",
    resolution_rule_attributes={
        "conditionType": "all",
        "conditionsAttributes": [
            {
                "field": "$.current_state",
                "kind": "payload",
                "operator": "is",
                "value": "UP",
            }
        ],
        "enabled": True,
        "identifierJsonPath": "$.check_id",
        "identifierReferenceKind": "payload",
    },
    secret=Output.secret(rootly_secrets["alert_source_secrets"]["pingdom"]),
    source_type="generic_webhook",
    sourceable_attributes={
        "autoResolve": True,
        "fieldMappingsAttributes": [
            {"field": "external_id", "jsonPath": "$.check_id"},
            {"field": "state", "jsonPath": "$.current_state"},
        ],
        "resolveState": "UP",
    },
    status="connected",
    webhook_endpoint="https://webhooks.rootly.com/webhooks/incoming/generic_webhooks/notify/<TYPE>/<ID>",
    opts=rootly_pingdom_alert_source_opts,
)

alerts_source_platform_engineering_team_email_monitor = rootly.AlertsSource(
    "platform-engineering-team-email-monitor",
    alert_source_fields_attributes=[
        {"alertFieldId": "39b50c54-efa8-47fc-acc6-90f1455a8834"},
        {
            "alertFieldId": "4a3add3c-5611-4dd9-ba65-4fb60b7f4fc6",
            "templateBody": "{{ alert.data.email.body }}",
        },
        {"alertFieldId": "45a09cf3-b0f2-43cf-b596-34aaab9279dc"},
    ],
    alert_urgency_id="5d357977-9dbe-42ad-b647-5a442cab3d96",
    deduplication_key_kind="payload",
    email="group-974a02b70e49057837449ca67e43c279@email.rootly.com",
    name="Platform Engineering Team Email Monitor",
    owner_group_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    secret=Output.secret(
        rootly_secrets["alert_source_secrets"][
            "platform_engineering_team_email_monitor"
        ]
    ),
    source_type="email",
    status="connected",
    opts=rootly_alert_source_opts,
)

alert_route_cloudwatch_catch_all_route = rootly.AlertRoute(
    "cloudwatch-catch-all-route",
    alerts_source_ids=["6e5745ec-7cef-4a20-a524-cb8829846167"],
    enabled=True,
    name="Cloudwatch Catch-All Route",
    owning_team_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    rules=[
        {
            "destinations": [
                {
                    "targetId": "96629210-cc41-4e57-b059-b182a0f01c5b",
                    "targetType": "EscalationPolicy",
                }
            ],
            "fallbackRule": True,
            "name": "Fallback Rule for Cloudwatch Catch-All Route",
            "position": 1,
        }
    ],
    opts=rootly_opts,
)

alert_route_grafana_production_catch_all_route = rootly.AlertRoute(
    "grafana-production-catch-all-route",
    alerts_source_ids=["90cda8ea-ff34-4553-b0c3-8744ee74200d"],
    enabled=True,
    name="Grafana Production Catch-All Route",
    owning_team_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    rules=[
        {
            "destinations": [
                {
                    "targetId": "96629210-cc41-4e57-b059-b182a0f01c5b",
                    "targetType": "EscalationPolicy",
                }
            ],
            "fallbackRule": True,
            "name": "Fallback Rule for Grafana Production Catch-All Route",
            "position": 1,
        }
    ],
    opts=rootly_opts,
)

alert_route_pingdom_catch_all_route = rootly.AlertRoute(
    "pingdom-catch-all-route",
    alerts_source_ids=["0b59c848-f764-4a31-a90f-1d473cc2b134"],
    enabled=True,
    name="Pingdom Catch-All Route",
    owning_team_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    rules=[
        {
            "destinations": [
                {
                    "targetId": "96629210-cc41-4e57-b059-b182a0f01c5b",
                    "targetType": "EscalationPolicy",
                }
            ],
            "fallbackRule": True,
            "name": "Fallback Rule for Pingdom Catch-All Route",
            "position": 1,
        }
    ],
    opts=rootly_opts,
)

alert_route_platform_engineering_team_email_monitor_route = rootly.AlertRoute(
    "platform-engineering-team-email-monitor-route",
    alerts_source_ids=["5933666e-e61e-4339-9b39-41756dafdd00"],
    enabled=True,
    name="Platform Engineering Team Email Monitor Route",
    owning_team_ids=["9f00e9f1-2f13-470e-a856-50ab5003f260"],
    rules=[
        {
            "destinations": [
                {
                    "targetId": "9f00e9f1-2f13-470e-a856-50ab5003f260",
                    "targetType": "Group",
                }
            ],
            "fallbackRule": True,
            "name": "Fallback Rule for Platform Engineering Team Email Monitor Route",
            "position": 1,
        }
    ],
    opts=rootly_opts,
)
