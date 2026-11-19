# /// script
# requires-python = ">=3.12"
# dependencies = [
#   # The deployed image's version (HINDSIGHT_VERSION in src/bridge/lib/versions.py).
#   "hindsight-api-slim==1.2.0",
#   "pytest>=8",
#   "pytest-asyncio>=0.24",
# ]
#
# [tool.uv]
# # 1.2.0 is the deployed image's version and is inside the repo's 7-day
# # exclude-newer window until 2026-11-10.
# exclude-newer-package = { hindsight-api-slim = "2026-11-04T00:00:00Z" }
# ///
"""Unit tests for ol_memory_ext.model_calls.EmbeddingMeter.

Runs against the Hindsight package, not a live server, and checks the counters
as /metrics renders them. See README.md.
"""

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prometheus_client import REGISTRY, generate_latest

sys.path.insert(
    0, str(Path(__file__).parents[2] / "src/ol_infrastructure/applications/hindsight")
)
from hindsight_api.extensions.model_call import ModelCall
from hindsight_api.extensions.operation_validator import RecallContext
from hindsight_api.metrics import initialize_metrics
from hindsight_api.models import RequestContext
from ol_memory_ext import LearnerBankValidator, caller_app, model_calls
from ol_memory_ext.model_calls import EmbeddingMeter

MODEL = "bedrock/cohere.embed-v4:0"
LEARNER = "0b6e1c4e-6d0a-4f43-9b38-2f1f6f2f9c11"
BANK = f"u-{LEARNER}"
LEARNER_TEXT = "my student number is 12345 and I struggle with recursion"


@pytest.fixture(scope="module")
def meter():
    # Built before the meter provider exists, as the engine does.
    meter = EmbeddingMeter({})
    initialize_metrics()
    return meter


def _call(**overrides) -> ModelCall:
    now = datetime.now(UTC)
    fields = {
        "id": str(uuid.uuid4()),
        "started_at": now,
        "ended_at": now,
        "duration_ms": 12,
        "tenant": "public",
        "bank_id": BANK,
        "operation": "retain",
        "trace_id": None,
        "scope": "embedding_document",
        "call_type": "embeddings",
        "provider": "litellm-sdk",
        "model": MODEL,
        "served_by": None,
        "success": True,
        "error": None,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "thoughts_tokens": 0,
        "input_count": 2,
        "finish_reason": None,
        "request_params": {},
        "metadata": {"document_id": "learn-ai.note-1"},
        "input": {"texts": ["a" * 10, "b" * 7]},
        "output": None,
        "tool_calls": None,
    }
    return ModelCall(**{**fields, **overrides})


def _value(name: str, **labels: str) -> float:
    # The exporter adds otel_scope_* labels of its own.
    wanted = {
        "app": "unknown",
        "operation": "retain",
        "model": MODEL,
        "success": "true",
    } | labels
    return sum(
        sample.value
        for family in REGISTRY.collect()
        for sample in family.samples
        if sample.name == name and wanted.items() <= sample.labels.items()
    )


def _snapshot(**labels: str) -> tuple[float, float, float]:
    return (
        _value("ol_memory_embedding_calls_total", **labels),
        _value("ol_memory_embedding_texts_total", **labels),
        _value("ol_memory_embedding_estimated_input_tokens_total", **labels),
    )


def _delta(before, after):
    return tuple(b - a for a, b in zip(before, after, strict=True))


def test_retain_is_counted_under_the_document_ids_app(meter):
    before = _snapshot(app="learn-ai")
    meter.record(_call())
    # 17 characters is 4.25 tokens, rounded up.
    assert _delta(before, _snapshot(app="learn-ai")) == (1, 2, 5)


def test_failed_call_is_labelled(meter):
    before = _snapshot(app="mit-learn", success="false")
    meter.record(
        _call(
            success=False,
            error="RuntimeError: boom",
            metadata={"document_id": "mit-learn.note-2"},
        )
    )
    assert _delta(before, _snapshot(app="mit-learn", success="false"))[0] == 1


def test_call_without_document_id_or_caller_is_unknown(meter):
    before = _snapshot(operation="recall")
    meter.record(_call(operation="recall", scope="embedding_query", metadata={}))
    assert _delta(before, _snapshot(operation="recall"))[0] == 1


@pytest.mark.asyncio
async def test_recall_takes_the_app_the_validator_passed(meter):
    validator = LearnerBankValidator({"database_url": "postgresql://unused"})
    rc = RequestContext(tenant_id="learn-ai", api_key_id=LEARNER)
    result = await validator.validate_recall(
        RecallContext(bank_id=BANK, query=LEARNER_TEXT, request_context=rc)
    )
    assert result.allowed
    labels = {"app": "learn-ai", "operation": "recall"}
    before = _snapshot(**labels)
    meter.record(_call(operation="recall", scope="embedding_query", metadata={}))
    assert _delta(before, _snapshot(**labels))[0] == 1


def test_other_call_types_are_ignored(meter):
    before = _snapshot(app="learn-ai")
    meter.record(_call(call_type="rerank"))
    assert _delta(before, _snapshot(app="learn-ai")) == (0, 0, 0)


def test_content_and_identifiers_stay_out_of_metrics(meter):
    meter.record(
        _call(
            metadata={"document_id": "learn-ai.private-doc-7"},
            input={"texts": [LEARNER_TEXT]},
        )
    )
    rendered = generate_latest(REGISTRY).decode()
    assert "ol_memory_embedding_calls_total" in rendered
    for forbidden in (LEARNER_TEXT, "private-doc-7", LEARNER):
        assert forbidden not in rendered


def test_record_never_raises(meter, monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise ValueError(LEARNER_TEXT)

    monkeypatch.setattr(model_calls, "_app", fail)
    caplog.set_level("WARNING", logger="ol_memory_ext.model_calls")
    meter.record(_call())
    assert "ValueError" in caplog.text
    assert LEARNER_TEXT not in caplog.text


@pytest.fixture(autouse=True)
def _reset_caller_app():
    token = caller_app.set(None)
    yield
    caller_app.reset(token)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
