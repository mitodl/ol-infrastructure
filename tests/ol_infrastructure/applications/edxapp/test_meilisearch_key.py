"""Unit tests for the edxapp Meilisearch API key derivation."""

# A getmeili/meilisearch:v1.56.2 container started with this master key returned
# this value for the key created with EDXAPP_MEILISEARCH_KEY_UID (2026-11-20).
MEILISEARCH_TEST_MASTER_KEY = (
    "test-master-key-0123456789abcdef"  # pragma: allowlist secret
)
MEILISEARCH_TEST_EDXAPP_KEY = "c968c9625bcdc21d6870616264c856d5833d92f0837bcbc131f805ff404c214b"  # pragma: allowlist secret  # noqa: E501


def test_derived_key_matches_meilisearch() -> None:
    # Imported here: meilisearch.py imports k8s_configmaps, which builds a boto
    # client at import and needs the AWS env that the autouse set_aws_env_vars
    # fixture sets after collection.
    from ol_infrastructure.applications.edxapp.meilisearch import (  # noqa: PLC0415
        edxapp_meilisearch_api_key,
    )

    assert (
        edxapp_meilisearch_api_key(MEILISEARCH_TEST_MASTER_KEY)
        == MEILISEARCH_TEST_EDXAPP_KEY
    )


def test_key_request_uses_the_derivation_uid() -> None:
    from ol_infrastructure.applications.edxapp.meilisearch import (  # noqa: PLC0415
        EDXAPP_MEILISEARCH_KEY_REQUEST,
        EDXAPP_MEILISEARCH_KEY_UID,
    )

    assert EDXAPP_MEILISEARCH_KEY_REQUEST["uid"] == EDXAPP_MEILISEARCH_KEY_UID
