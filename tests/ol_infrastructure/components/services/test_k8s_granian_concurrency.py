"""Tests for GranianConfig concurrency resolution.

Covers the blocking_threads/backpressure resolution matrix (wsgi/asgi x
None/explicit), the CLI defaults that track Granian's own, and the synth-time
workers_max_rss derivation from the container memory limit.
"""

from __future__ import annotations

import pytest
from kubernetes.utils.quantity import parse_quantity
from pydantic import ValidationError

from bridge.lib.magic_numbers import (
    DEFAULT_WSGI_BACKPRESSURE_MULTIPLIER,
    DEFAULT_WSGI_BLOCKING_THREADS,
)
from ol_infrastructure.components.services.k8s import GranianConfig


def arg_value(args: list[str], flag: str) -> str | None:
    """Return the value following ``flag`` in a granian arg list, or None."""
    return args[args.index(flag) + 1] if flag in args else None


# ─── CLI defaults ─────────────────────────────────────────────────────────────


def test_defaults_track_granian_cli():
    gc = GranianConfig()
    assert gc.workers == 1
    assert gc.runtime_threads == 1
    assert gc.runtime_mode is None
    assert gc.backlog == 128


def test_runtime_mode_omitted_when_none():
    args = GranianConfig().build_args()
    assert "--runtime-mode" not in args
    assert arg_value(args, "--runtime-threads") == "1"
    assert arg_value(args, "--workers") == "1"


# ─── blocking_threads / backpressure resolution ───────────────────────────────


def test_wsgi_defaults_resolve_at_config_time():
    gc = GranianConfig()
    assert gc.blocking_threads == DEFAULT_WSGI_BLOCKING_THREADS
    assert gc.backpressure == (
        DEFAULT_WSGI_BACKPRESSURE_MULTIPLIER * DEFAULT_WSGI_BLOCKING_THREADS
    )


def test_wsgi_defaults_emitted():
    args = GranianConfig().build_args()
    assert arg_value(args, "--blocking-threads") == str(DEFAULT_WSGI_BLOCKING_THREADS)
    assert arg_value(args, "--backpressure") == str(
        DEFAULT_WSGI_BACKPRESSURE_MULTIPLIER * DEFAULT_WSGI_BLOCKING_THREADS
    )


def test_wsgi_explicit_blocking_threads_drives_backpressure():
    gc = GranianConfig(blocking_threads=4)
    assert gc.blocking_threads == 4
    assert gc.backpressure == 8


def test_wsgi_explicit_backpressure_preserved():
    gc = GranianConfig(blocking_threads=4, backpressure=64)
    args = gc.build_args()
    assert arg_value(args, "--blocking-threads") == "4"
    assert arg_value(args, "--backpressure") == "64"


def test_wsgi_backpressure_alone_does_not_change_blocking_threads():
    gc = GranianConfig(backpressure=32)
    assert gc.blocking_threads == DEFAULT_WSGI_BLOCKING_THREADS
    assert gc.backpressure == 32


@pytest.mark.parametrize("interface", ["asgi", "asginl"])
def test_async_interfaces_omit_both_flags_by_default(interface):
    gc = GranianConfig(interface=interface, no_ws=False)
    assert gc.blocking_threads is None
    assert gc.backpressure is None
    args = gc.build_args()
    assert "--blocking-threads" not in args
    assert "--backpressure" not in args


@pytest.mark.parametrize("interface", ["asgi", "asginl"])
def test_async_interfaces_reject_multiple_blocking_threads(interface):
    with pytest.raises(ValidationError, match="forces blocking_threads=1"):
        GranianConfig(interface=interface, blocking_threads=8)


@pytest.mark.parametrize("interface", ["asgi", "asginl"])
def test_async_interfaces_accept_blocking_threads_of_one(interface):
    """1 matches what Granian does anyway, so it is redundant rather than wrong."""
    gc = GranianConfig(interface=interface, blocking_threads=1)
    assert arg_value(gc.build_args(), "--blocking-threads") == "1"


@pytest.mark.parametrize("interface", ["asgi", "asginl"])
def test_async_interfaces_allow_explicit_backpressure(interface):
    gc = GranianConfig(interface=interface, backpressure=64)
    assert arg_value(gc.build_args(), "--backpressure") == "64"


# ─── workers_max_rss ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("memory_limit", "workers", "expected_mib"),
    [
        ("1200Mi", 1, 1080),
        ("1Gi", 1, 921),
        ("2000Mi", 2, 900),
    ],
)
def test_workers_max_rss_derivation(memory_limit, workers, expected_mib):
    """Mirrors the synth-time formula in OLApplicationK8s.

    floor(limit_bytes / workers * 0.9) MiB, against the pod's *current* declared
    limit -- not the VPA ceiling, which the kernel OOM killer does not enforce.
    """
    limit_bytes = int(parse_quantity(memory_limit))
    computed = max(1, int(limit_bytes / workers * 0.9) // (1024 * 1024))
    assert computed == expected_mib


def test_workers_max_rss_explicit_is_emitted_verbatim():
    args = GranianConfig(workers_max_rss=2880).build_args()
    assert arg_value(args, "--workers-max-rss") == "2880"


def test_workers_max_rss_absent_when_unresolved():
    """The component resolves this at synth time; the model alone emits nothing."""
    assert "--workers-max-rss" not in GranianConfig().build_args()
