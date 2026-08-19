"""Tests for the OLGCPProject component and its configuration models.

The invariants under test are the ones the credential inventory in
``docs/plans/gcp-service-account-consumer-map.md`` showed the legacy estate
violating: unrestricted API keys, and services that could be disabled out from
under a consumer that emits no measurable traffic.
"""

import pulumi
import pytest

from ol_infrastructure.components.gcp.project import (
    OLGCPAPIKeyConfig,
    OLGCPProject,
    OLGCPProjectConfig,
    OLGCPServiceAccountConfig,
    adoption_opts,
)
from ol_infrastructure.lib.ol_types import GCPBase


def valid_labels() -> dict[str, str]:
    return {"ou": "operations", "environment": "qa"}


class TestGCPBaseLabels:
    """GCP labels have a much narrower character set than AWS tags."""

    def test_required_labels_enforced(self):
        with pytest.raises(ValueError, match="Missing labels"):
            GCPBase(project_id="test-project", labels={"ou": "operations"})

    def test_invalid_business_unit_rejected(self):
        with pytest.raises(ValueError, match="not a valid business unit"):
            GCPBase(
                project_id="test-project",
                labels={"ou": "not-a-real-unit", "environment": "qa"},
            )

    def test_keys_and_values_are_lowercased(self):
        """``OU``/``Production`` are valid AWS tags but invalid GCP labels."""
        config = GCPBase(
            project_id="test-project",
            labels={"OU": "operations", "Environment": "Production"},
        )
        assert config.labels["ou"] == "operations"
        assert config.labels["environment"] == "production"

    def test_invalid_label_value_rejected(self):
        with pytest.raises(ValueError, match="not a valid GCP label value"):
            GCPBase(
                project_id="test-project",
                labels={
                    "ou": "operations",
                    "environment": "qa",
                    "owner": "person@mit.edu",
                },
            )

    def test_pulumi_managed_label_added(self):
        config = GCPBase(project_id="test-project", labels=valid_labels())
        assert config.labels["pulumi_managed"] == "true"

    def test_merged_labels_normalizes_additions(self):
        config = GCPBase(project_id="test-project", labels=valid_labels())
        merged = config.merged_labels({"Application": "OCW-Studio"})
        assert merged["application"] == "ocw-studio"
        assert merged["ou"] == "operations"

    def test_merged_labels_rejects_invalid_additions(self):
        """Additions bypass the constructor, so merging has to re-validate."""
        config = GCPBase(project_id="test-project", labels=valid_labels())
        with pytest.raises(ValueError, match="not a valid GCP label value"):
            config.merged_labels({"owner": "person@mit.edu"})
        with pytest.raises(ValueError, match="not a valid GCP label key"):
            config.merged_labels({"9lives": "cat"})


class TestAPIKeyRestrictions:
    """An API key with no restriction is a config error, not a default."""

    def test_unrestricted_key_rejected(self):
        with pytest.raises(ValueError, match="declares no restrictions"):
            OLGCPAPIKeyConfig(
                key_name="server-key-1",
                display_name="Server key 1",
                restrictions={},
            )

    def test_empty_restriction_values_count_as_unrestricted(self):
        with pytest.raises(ValueError, match="declares no restrictions"):
            OLGCPAPIKeyConfig(
                key_name="server-key-1",
                display_name="Server key 1",
                restrictions={"api_targets": [], "browser_key_restrictions": None},
            )

    def test_unknown_restriction_rejected(self):
        with pytest.raises(ValueError, match="Unknown API key restriction"):
            OLGCPAPIKeyConfig(
                key_name="youtube-key",
                display_name="YouTube key",
                restrictions={"referrer_restrictions": ["mit.edu"]},
            )

    def test_empty_unknown_restriction_still_rejected(self):
        """A misspelled key with no value must not slip past to the provider."""
        with pytest.raises(ValueError, match="Unknown API key restriction"):
            OLGCPAPIKeyConfig(
                key_name="youtube-key",
                display_name="YouTube key",
                restrictions={
                    "api_targets": [{"service": "youtube.googleapis.com"}],
                    "brower_key_restrictions": None,
                },
            )

    def test_api_target_restriction_accepted(self):
        config = OLGCPAPIKeyConfig(
            key_name="youtube-key",
            display_name="MIT Open Youtube API Key - Production",
            restrictions={"api_targets": [{"service": "youtube.googleapis.com"}]},
        )
        assert config.restrictions["api_targets"][0]["service"] == (
            "youtube.googleapis.com"
        )


class TestOLGCPProject:
    """Resource-level behaviour, checked through Pulumi mocks."""

    @staticmethod
    def build_component() -> OLGCPProject:
        return OLGCPProject(
            "test-gcp-project",
            OLGCPProjectConfig(
                project_id="test-project",
                labels=valid_labels(),
                enabled_services=["youtube.googleapis.com", "drive.googleapis.com"],
                service_accounts=[
                    OLGCPServiceAccountConfig(
                        account_id="ocw-studio-production",
                        display_name="OCW Studio Production",
                        project_roles=["roles/storage.objectViewer"],
                    )
                ],
                api_keys=[
                    OLGCPAPIKeyConfig(
                        key_name="youtube",
                        display_name="YouTube key",
                        restrictions={
                            "api_targets": [{"service": "youtube.googleapis.com"}]
                        },
                    )
                ],
            ),
        )

    @pulumi.runtime.test
    def test_services_are_never_disabled_on_destroy(self):
        component = self.build_component()

        def check(disable_on_destroy):
            # Removing a service from this stack must not turn the API off for
            # consumers the stack does not know about.
            assert disable_on_destroy is False

        return pulumi.Output.all(
            *[service.disable_on_destroy for service in component.services.values()]
        ).apply(lambda values: [check(value) for value in values])

    @pulumi.runtime.test
    def test_service_account_email_exported(self):
        component = self.build_component()

        def check(email):
            assert email == (
                "ocw-studio-production@test-project.iam.gserviceaccount.com"
            )

        return component.service_account_emails["ocw-studio-production"].apply(check)

    def test_all_declared_resources_are_created(self):
        component = self.build_component()
        assert set(component.services) == {
            "youtube.googleapis.com",
            "drive.googleapis.com",
        }
        assert set(component.service_accounts) == {"ocw-studio-production"}
        assert set(component.api_keys) == {"youtube"}


class TestAdoption:
    """The invariant the whole design rests on: adopt, never recreate.

    A resource carrying an ``import_id`` predates this stack and has consumers
    the stack does not know about, so it must be imported rather than created,
    and protected so that a diff resolving to a replacement -- which for a
    service account or an API key means the credential value changes -- fails
    loudly instead of proceeding.
    """

    SA_IMPORT_ID = (
        "projects/test-project/serviceAccounts/"
        "legacy-sa@test-project.iam.gserviceaccount.com"
    )
    KEY_IMPORT_ID = "projects/123456789/locations/global/keys/legacy-key-name"

    def test_import_id_sets_both_import_and_protect(self):
        opts = adoption_opts(pulumi.ResourceOptions(), self.SA_IMPORT_ID)
        assert opts.import_ == self.SA_IMPORT_ID
        assert opts.protect is True

    def test_no_import_id_leaves_options_untouched(self):
        base = pulumi.ResourceOptions()
        opts = adoption_opts(base, None)
        assert opts is base
        assert opts.import_ is None
        assert opts.protect is None

    @staticmethod
    def build_component(*, adopt: bool) -> OLGCPProject:
        return OLGCPProject(
            f"test-adopt-{adopt}",
            OLGCPProjectConfig(
                project_id="test-project",
                labels=valid_labels(),
                service_accounts=[
                    OLGCPServiceAccountConfig(
                        account_id="legacy-sa",
                        display_name="Legacy SA",
                        import_id=TestAdoption.SA_IMPORT_ID if adopt else None,
                    )
                ],
                api_keys=[
                    OLGCPAPIKeyConfig(
                        key_name="legacy-key",
                        display_name="Legacy key",
                        restrictions={
                            "api_targets": [{"service": "youtube.googleapis.com"}]
                        },
                        import_id=TestAdoption.KEY_IMPORT_ID if adopt else None,
                    )
                ],
            ),
        )

    @pulumi.runtime.test
    def test_adopted_resources_are_protected(self):
        component = self.build_component(adopt=True)
        # _protect is where Pulumi stores ResourceOptions.protect on the
        # resource; import_ is consumed by the engine and not retained, which
        # is why it is asserted against adoption_opts above.
        assert component.service_accounts["legacy-sa"]._protect is True
        assert component.api_keys["legacy-key"]._protect is True

    @pulumi.runtime.test
    def test_newly_created_resources_are_not_protected(self):
        component = self.build_component(adopt=False)
        assert component.service_accounts["legacy-sa"]._protect is not True
        assert component.api_keys["legacy-key"]._protect is not True
