"""The model stub learn-ai calls in local-dev, served on an ephemeral port."""

import importlib.util
import json
import threading
import urllib.request
from typing import Any

import pytest

from tests.local_dev.program_render import REPO

SERVER = REPO / "local-dev/apps/learn-ai/llm-stub/server.py"
# A shortened form of open-learning-ai-tutor's assessment instruction, keeping
# the "selection" key the stub recognizes it by.
TUTOR_ASSESSMENT_PROMPT = (
    "Answer in the following JSON format ONLY and do not output anything else:\n"
    '{\n    "justification": "..",\n    "selection": ".."\n}'
)


@pytest.fixture(scope="module")
def stub_url():
    spec = importlib.util.spec_from_file_location("llm_stub_server", SERVER)
    assert spec
    assert spec.loader
    # Loaded from a path, so mypy knows none of its names.
    server_module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_module)
    server_module.WORD_DELAY_SECONDS = 0
    server = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
    # shutdown() returns only at serve_forever's next poll, so the 0.5s default
    # poll interval added half a second to every test's teardown.
    threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    ).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _events(stub_url, content, **body):
    request = urllib.request.Request(  # noqa: S310
        f"{stub_url}/chat/completions",
        data=json.dumps(
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": content}],
                "stream": True,
                **body,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        lines = response.read().decode().splitlines()
    return [line.removeprefix("data: ") for line in lines if line.startswith("data: ")]


def _text(chunks):
    return "".join(
        chunk["choices"][0]["delta"].get("content", "")
        for chunk in chunks
        if chunk["choices"]
    )


def test_a_streamed_reply_says_how_to_switch_then_reports_usage(stub_url):
    events = _events(stub_url, "hello", stream_options={"include_usage": True})
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    text = _text(chunks)
    assert "`gpt-4o-mini`" in text
    assert "learn_ai:env_vars" in text
    finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]]
    assert finishes.count("stop") == 1
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["completion_tokens"] > 0


def test_the_tutors_assessment_gets_the_asking_for_concepts_code(stub_url):
    chunks = [json.loads(e) for e in _events(stub_url, TUTOR_ASSESSMENT_PROMPT)[:-1]]
    # Assessment.ASKING_FOR_CONCEPTS in open-learning-ai-tutor 0.2.16.
    assert json.loads(_text(chunks))["selection"] == "l"
