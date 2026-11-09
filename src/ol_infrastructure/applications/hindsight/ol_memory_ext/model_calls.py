"""Per-app metering of Hindsight's embedding calls, with none of their content.

``EmbeddingMeter`` is the model call extension
(``HINDSIGHT_API_MODEL_CALL_EXTENSION``). For each embedding call it adds to
three counters on the OpenTelemetry meter Hindsight serves at /metrics:

- ``ol_memory.embedding.calls``: calls made;
- ``ol_memory.embedding.texts``: texts sent;
- ``ol_memory.embedding.estimated_input_tokens``: input characters / 4. Bedrock
  bills Cohere Embed v4 by input token and LiteLLM reports none for it, so this
  is an estimate; the dollar figure comes from Cost Explorer.

Each is labelled with ``app``, ``operation`` (retain, recall, ...), ``model`` and
``success``. The app is the prefix of the call's document id, which the
validator requires to be the writing app's name, or else the app the validator
passed for this request (``caller_app``), or ``unknown``. No input text, bank,
learner or document id goes into a label or a log line.

Kept out of the package's ``__init__``, which the other extensions load:
Hindsight's model_call module imports the engine package, and memory_engine.py
defers its own import of it to avoid an import cycle.
"""

import logging
import math
from typing import Any

from hindsight_api.extensions.model_call import ModelCall, ModelCallExtension
from ol_memory_ext import caller_app
from opentelemetry import metrics

logger = logging.getLogger(__name__)

EMBEDDINGS_CALL_TYPE = "embeddings"
CHARS_PER_TOKEN = 4
UNKNOWN_APP = "unknown"


def _app(call: ModelCall) -> str:
    document_id = call.metadata.get("document_id")
    if isinstance(document_id, str) and "." in document_id:
        return document_id.split(".", 1)[0]
    return caller_app.get() or UNKNOWN_APP


def _input_chars(call_input: Any) -> int:
    texts = call_input.get("texts") if isinstance(call_input, dict) else None
    return sum(len(text) for text in texts or () if isinstance(text, str))


class EmbeddingMeter(ModelCallExtension):
    """Count embedding calls, texts and estimated input tokens per app."""

    def __init__(self, config: dict[str, str]):
        """Create the counters.

        The engine loads extensions before the server sets the global meter
        provider; the API's proxy instruments bind to it once it is set.
        """
        super().__init__(config)
        meter = metrics.get_meter(__name__)
        self._calls = meter.create_counter(
            "ol_memory.embedding.calls", description="Embedding calls"
        )
        self._texts = meter.create_counter(
            "ol_memory.embedding.texts", description="Texts sent to the embedding model"
        )
        self._estimated_tokens = meter.create_counter(
            "ol_memory.embedding.estimated_input_tokens",
            description=(
                f"Input tokens sent to the embedding model, estimated as characters"
                f" / {CHARS_PER_TOKEN}"
            ),
        )

    def record(self, call: ModelCall) -> None:
        """Add one finished call to the counters.

        Runs on the call path, so it only adds. A failure is logged and dropped:
        metering must never fail a learner's retain or recall. The log line names
        the exception type alone, since a message could quote the input.
        """
        try:
            if call.call_type != EMBEDDINGS_CALL_TYPE:
                return
            attributes = {
                "app": _app(call),
                "operation": call.operation or "none",
                "model": call.model,
                "success": "true" if call.success else "false",
            }
            self._calls.add(1, attributes)
            self._texts.add(call.input_count or 0, attributes)
            self._estimated_tokens.add(
                math.ceil(_input_chars(call.input) / CHARS_PER_TOKEN), attributes
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("embedding metering failed: %s", type(exc).__name__)
