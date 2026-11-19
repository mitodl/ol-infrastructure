"""Helpers shared by the tests that inspect a rendered pipeline's job plans."""

from collections.abc import Iterator
from typing import Any


def walk(steps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield every step, including those nested in do/try/in_parallel blocks."""
    for step in steps:
        yield step
        for key in ("do", "in_parallel"):
            nested = step.get(key)
            if isinstance(nested, dict):
                nested = nested.get("steps")
            if nested:
                yield from walk(nested)
        if isinstance(step.get("try"), dict):
            yield from walk([step["try"]])
