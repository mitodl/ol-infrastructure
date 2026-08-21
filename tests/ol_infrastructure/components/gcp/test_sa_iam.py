"""Tests for service-account-level IAM grants.

These are grants held *on* an account rather than *by* it, they do not appear
in the project IAM policy, and they are the two pieces of the federation wiring
most likely to survive only as undocumented console clicks: who may exchange an
external identity for this account's token, and who may impersonate it.
"""

import pulumi
import pytest

from ol_infrastructure.components.gcp.project import (
    OLGCPProject,
    OLGCPProjectConfig,
    OLGCPServiceAccountConfig,
    OLGCPServiceAccountIAMMemberConfig,
)

PRINCIPAL_SET = (
    "principalSet://iam.googleapis.com/projects/32631020496/locations/global/"
    "workloadIdentityPools/ol-infrastructure/attribute.concourse_env/production"
)


def valid_labels() -> dict[str, str]:
    return {"ou": "operations", "environment": "production"}


def build_component(grants: list[OLGCPServiceAccountIAMMemberConfig]) -> OLGCPProject:
    return OLGCPProject(
        "test-sa-iam",
        OLGCPProjectConfig(
            project_id="mitol01",
            labels=valid_labels(),
            service_accounts=[
                OLGCPServiceAccountConfig(
                    account_id="pulumi-gcp",
                    display_name="Pulumi GCP provider",
                    iam_members=grants,
                )
            ],
        ),
    )


class TestServiceAccountIAMMembers:
    """The component accepts both grant shapes without special-casing either."""

    def test_workload_identity_and_token_creator_together(self):
        component = build_component(
            [
                OLGCPServiceAccountIAMMemberConfig(
                    role="roles/iam.workloadIdentityUser", member=PRINCIPAL_SET
                ),
                OLGCPServiceAccountIAMMemberConfig(
                    role="roles/iam.serviceAccountTokenCreator",
                    member="group:platform-engineering@mit.edu",
                ),
            ]
        )
        assert set(component.service_accounts) == {"pulumi-gcp"}

    def test_no_grants_is_valid(self):
        """Most accounts hold none of these; the field must stay optional."""
        component = build_component([])
        assert set(component.service_accounts) == {"pulumi-gcp"}

    def test_repeated_role_does_not_collide(self):
        """Two members of one role must not produce duplicate resource names."""
        component = build_component(
            [
                OLGCPServiceAccountIAMMemberConfig(
                    role="roles/iam.serviceAccountTokenCreator",
                    member="group:platform-engineering@mit.edu",
                ),
                OLGCPServiceAccountIAMMemberConfig(
                    role="roles/iam.serviceAccountTokenCreator",
                    member="group:data-engineering@mit.edu",
                ),
            ]
        )
        assert set(component.service_accounts) == {"pulumi-gcp"}

    @pytest.mark.parametrize(
        "role",
        ["roles/iam.workloadIdentityUser", "roles/iam.serviceAccountTokenCreator"],
    )
    def test_grant_config_round_trips(self, role):
        grant = OLGCPServiceAccountIAMMemberConfig(role=role, member=PRINCIPAL_SET)
        assert grant.role == role
        assert grant.member == PRINCIPAL_SET
        assert grant.import_id is None


@pulumi.runtime.test
def test_service_account_id_is_the_resource_name_not_the_email():
    """gcp.serviceaccount.IAMMember keys on the account's resource name.

    Passing the email would silently address nothing, so this pins the wiring.
    """
    component = build_component(
        [
            OLGCPServiceAccountIAMMemberConfig(
                role="roles/iam.workloadIdentityUser", member=PRINCIPAL_SET
            )
        ]
    )

    def check(account_name):
        assert account_name.startswith("projects/")
        assert "/serviceAccounts/" in account_name

    return component.service_accounts["pulumi-gcp"].name.apply(check)
