"""An OpenAI-compatible chat model for learn-ai in local-dev: canned replies.

learn-ai reaches it through OPENAI_BASE_URL, which the learn-ai program sets
unless the developer's learn_ai:env_vars picks a model backend. It serves only
streamed Chat Completions: learn-ai builds every model with streaming=True, and
LiteLLM keeps a custom base URL on Chat Completions.
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 8099
# Slow enough that streaming is visible in a chat UI.
WORD_DELAY_SECONDS = 0.02
LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua."
)
# open-learning-ai-tutor's assessment prompt asks for JSON with this key; its
# prose prompt does not.
TUTOR_ASSESSMENT_KEY = '"selection"'
# Assessment.ASKING_FOR_CONCEPTS in open-learning-ai-tutor 0.2.16, the code the
# library falls back to when it cannot parse an assessment.
ASKING_FOR_CONCEPTS = "l"


def _texts(messages: list[dict[str, Any]]) -> list[str]:
    texts = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part.get("text", "") for part in content)
    return texts


def reply(model: str, messages: list[dict[str, Any]]) -> str:
    """Return the canned reply to a request for ``model``."""
    if any(TUTOR_ASSESSMENT_KEY in text for text in _texts(messages)):
        return json.dumps({"justification": LOREM, "selection": ASKING_FOR_CONCEPTS})
    return (
        f"**Local-dev stub model** (`{model}`): no model was called and no tools "
        "ran. For real answers, set `OPENAI_API_KEY` under `learn_ai:env_vars` in "
        "`local-dev/apps/learn-ai/pulumi/Pulumi.Dev.yaml`.\n\n"
        f"{LOREM}\n\n"
        "- Ut enim ad minim veniam\n"
        "- Quis nostrud exercitation\n"
        "- Duis aute irure dolor\n"
    )


class Handler(BaseHTTPRequestHandler):
    """Serve /health and streamed chat completions.

    A stream has no Content-Length and ends when the connection closes, which
    the handler's default HTTP/1.0 does after each response.
    """

    def do_GET(self) -> None:
        """Answer the readiness probe."""
        if self.path != "/health":
            sys.stderr.write(f"no route for GET {self.path}\n")
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def do_POST(self) -> None:
        """Stream the canned reply as Server-Sent Events."""
        if not self.path.endswith("/chat/completions"):
            sys.stderr.write(f"no route for POST {self.path}\n")
            self.send_response(404)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model, messages = body.get("model", ""), body.get("messages", [])
        sys.stderr.write(
            f"completion model={model} messages={len(messages)} "
            f"tools={len(body.get('tools') or [])}\n"
        )
        text = reply(model, messages)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "chatcmpl-local-dev-stub",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
        }
        for word in re.findall(r"\S+\s*", text):
            self._send(
                chunk,
                [{"index": 0, "delta": {"content": word}, "finish_reason": None}],
            )
            time.sleep(WORD_DELAY_SECONDS)
        self._send(chunk, [{"index": 0, "delta": {}, "finish_reason": "stop"}])
        if (body.get("stream_options") or {}).get("include_usage"):
            prompt_tokens = sum(len(t.split()) for t in _texts(messages))
            completion_tokens = len(text.split())
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            self._send({**chunk, "usage": usage}, [])
        self.wfile.write(b"data: [DONE]\n\n")

    def _send(self, chunk: dict[str, Any], choices: list[dict[str, Any]]) -> None:
        self.wfile.write(
            f"data: {json.dumps({**chunk, 'choices': choices})}\n\n".encode()
        )
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Log nothing per request.

        do_POST writes its own line, and the default one would repeat it and log
        every readiness probe.
        """


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
