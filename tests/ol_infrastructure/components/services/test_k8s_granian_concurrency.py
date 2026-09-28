"""Tests for GranianConfig concurrency resolution.

Covers the blocking_threads/backpressure resolution matrix (wsgi/asgi x
None/explicit), the CLI defaults that track Granian's own, and the synth-time
workers_max_rss derivation from the container memory limit.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bridge.lib.magic_numbers import (
    DEFAULT_WSGI_BACKPRESSURE,
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
    assert gc.backpressure == DEFAULT_WSGI_BACKPRESSURE


def test_wsgi_defaults_emitted():
    args = GranianConfig().build_args()
    assert arg_value(args, "--blocking-threads") == str(DEFAULT_WSGI_BLOCKING_THREADS)
    assert arg_value(args, "--backpressure") == str(DEFAULT_WSGI_BACKPRESSURE)


def test_wsgi_blocking_threads_does_not_drive_backpressure():
    """The connection budget is independent of the Python thread pool.

    Deriving one from the other is what let idle APISix keepalives exhaust the
    budget and take xpro down on 2026-08-31; see GranianConfig.backpressure.
    """
    gc = GranianConfig(blocking_threads=4)
    assert gc.blocking_threads == 4
    assert gc.backpressure == DEFAULT_WSGI_BACKPRESSURE


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


def test_holding_pins_reproduce_granians_old_derivation():
    """Callers awaiting their rollout stage pin the values Granian used to derive.

    Granian's own resolution is ``backpressure = backlog // workers`` and, for WSGI,
    ``blocking_threads = backpressure // 2``. At the pre-overhaul defaults
    (backlog=128, workers=2) that is 64 and 32 -- which is what the pin blocks in
    micromasters/xpro/ocw_studio/odl_video_service/mitxonline/edxapp carry, so
    landing the component change does not alter their behavior.
    """
    backlog, workers = 128, 2
    granian_backpressure = max(1, backlog // workers)
    granian_blocking_threads = max(1, granian_backpressure // 2)
    assert (granian_blocking_threads, granian_backpressure) == (32, 64)

    args = GranianConfig(
        workers=workers,
        backlog=backlog,
        blocking_threads=granian_blocking_threads,
        backpressure=granian_backpressure,
    ).build_args()
    assert arg_value(args, "--blocking-threads") == "32"
    assert arg_value(args, "--backpressure") == "64"


# ─── workers_max_rss ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("memory_limit", "workers", "startup_rss", "expected_mib"),
    [
        ("1200Mi", 1, None, 1080),
        ("1Gi", 1, None, 921),
        ("2000Mi", 2, None, 900),
        ("2800Mi", 1, None, 2520),
        # Headroom for the replacement worker during a planned respawn.
        ("3Gi", 1, 1100, 1664),
        ("2800Mi", 1, 600, 1920),
        ("4Gi", 2, 900, 1393),
    ],
)
def test_workers_max_rss_derivation(memory_limit, workers, startup_rss, expected_mib):
    """Derived from the pod's *current* declared limit, not the VPA ceiling."""
    gc = GranianConfig(workers=workers, worker_startup_rss=startup_rss)
    resolved = gc.resolve_workers_max_rss(memory_limit)
    assert resolved.workers_max_rss == expected_mib
    assert arg_value(resolved.build_args(), "--workers-max-rss") == str(expected_mib)


def test_workers_max_rss_overlap_fits_the_budget():
    """Every worker at the cap plus a fresh worker stays within 90% of the limit."""
    gc = GranianConfig(workers=2, worker_startup_rss=700).resolve_workers_max_rss("3Gi")
    assert gc.workers * gc.workers_max_rss + 700 <= 0.9 * 3072


def test_workers_max_rss_refuses_a_cap_below_startup_rss():
    with pytest.raises(ValueError, match="respawns continuously"):
        GranianConfig(worker_startup_rss=1100).resolve_workers_max_rss("2Gi")


def test_workers_max_rss_resolution_keeps_explicit_or_disabled_caps():
    assert (
        GranianConfig(workers_max_rss=1500)
        .resolve_workers_max_rss("3Gi")
        .workers_max_rss
        == 1500
    )
    assert (
        GranianConfig(limit_workers_max_rss=False)
        .resolve_workers_max_rss("3Gi")
        .workers_max_rss
        is None
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workers_max_rss": 1500},
        {"limit_workers_max_rss": False},
    ],
)
def test_worker_startup_rss_rejected_when_it_would_be_ignored(kwargs):
    with pytest.raises(ValidationError, match="worker_startup_rss"):
        GranianConfig(worker_startup_rss=600, **kwargs)


def test_workers_max_rss_explicit_is_emitted_verbatim():
    args = GranianConfig(workers_max_rss=2880).build_args()
    assert arg_value(args, "--workers-max-rss") == "2880"


def test_workers_max_rss_absent_when_unresolved():
    """The component resolves this at synth time; the model alone emits nothing."""
    assert "--workers-max-rss" not in GranianConfig().build_args()


# ─── respawn_interval ─────────────────────────────────────────────────────────


def test_respawn_interval_omitted_by_default():
    assert "--respawn-interval" not in GranianConfig().build_args()


def test_respawn_interval_emitted():
    args = GranianConfig(respawn_interval=60).build_args()
    assert arg_value(args, "--respawn-interval") == "60.0"
