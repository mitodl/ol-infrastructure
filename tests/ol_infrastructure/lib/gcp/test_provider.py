"""Tests for GCP provider credential resolution.

The invariant: which identity a stack runs as is never inherited from the
ambient environment. Either a credential document says so explicitly, or the
operator names an impersonation target explicitly.
"""

import asyncio
import json

import pulumi
import pytest

from ol_infrastructure.lib.gcp import provider as gcp_provider_lib

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class ProviderMocks(pulumi.runtime.Mocks):
    """Record the inputs each provider is constructed with."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation."""
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        """Mock data source calls. Nothing here invokes one."""
        return {}


@pytest.fixture(autouse=True)
def provider_mocks():
    """Install Pulumi mocks for each test."""
    pulumi.runtime.set_mocks(ProviderMocks())


WIF_DOCUMENT = json.dumps(
    {
        "type": "external_account",
        "audience": (
            "//iam.googleapis.com/projects/32631020496/locations/global/"
            "workloadIdentityPools/ol-infrastructure/providers/concourse"
        ),
        "service_account_impersonation_url": (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            "pulumi-gcp@mitol01.iam.gserviceaccount.com:generateAccessToken"
        ),
    }
)
SA_KEY_DOCUMENT = json.dumps({"type": "service_account", "project_id": "mitol01"})


class TestCredentialType:
    """The document declares its own shape; the loader does not guess."""

    def test_workload_identity_recognized(self):
        assert gcp_provider_lib.credential_type(WIF_DOCUMENT) == "external_account"

    def test_service_account_key_recognized(self):
        assert gcp_provider_lib.credential_type(SA_KEY_DOCUMENT) == "service_account"

    def test_missing_type_is_empty(self):
        assert gcp_provider_lib.credential_type(json.dumps({})) == ""


class TestGCPProvider:
    """Credential selection, including the local-impersonation escape hatch."""

    def test_unsupported_credential_type_rejected(self):
        with pytest.raises(ValueError, match="Unsupported GCP credential type"):
            gcp_provider_lib.gcp_provider(
                "test-provider",
                project="mitol01",
                credentials=json.dumps({"type": "authorized_user"}),
            )

    @pulumi.runtime.test
    def test_workload_identity_document_accepted(self):
        provider = gcp_provider_lib.gcp_provider(
            "test-provider", project="mitol01", credentials=WIF_DOCUMENT
        )
        return provider.project.apply(lambda project: (project == "mitol01").__bool__())

    @pulumi.runtime.test
    def test_impersonation_env_var_skips_the_sops_read(self, monkeypatch):
        """The local path must not need KMS access just to run a preview."""

        def explode() -> str:
            msg = "read_gcp_credentials should not be called when impersonating"
            raise AssertionError(msg)

        monkeypatch.setattr(gcp_provider_lib, "read_gcp_credentials", explode)
        monkeypatch.setenv(
            gcp_provider_lib.IMPERSONATION_ENV_VAR,
            "pulumi-gcp@mitol01.iam.gserviceaccount.com",
        )
        provider = gcp_provider_lib.gcp_provider("test-provider", project="mitol01")

        def check(target):
            assert target == "pulumi-gcp@mitol01.iam.gserviceaccount.com"

        return provider.impersonate_service_account.apply(check)

    @pulumi.runtime.test
    def test_explicit_credentials_beat_the_env_var(self, monkeypatch):
        """A caller that resolved a credential is never silently redirected."""
        monkeypatch.setenv(
            gcp_provider_lib.IMPERSONATION_ENV_VAR,
            "pulumi-gcp@mitol01.iam.gserviceaccount.com",
        )
        provider = gcp_provider_lib.gcp_provider(
            "test-provider", project="mitol01", credentials=WIF_DOCUMENT
        )

        def check(target):
            assert target is None

        return provider.impersonate_service_account.apply(check)
