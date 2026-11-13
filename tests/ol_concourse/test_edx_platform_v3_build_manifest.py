"""The edxapp pipeline refuses a lehrer manifest it can't read or that lacks a cell."""

from pathlib import Path

import pytest
import yaml

from bridge.settings.openedx.accessors import filter_deployments_by_release
from bridge.settings.openedx.types import OpenEdxSupportedRelease
from ol_concourse.pipelines.open_edx.edx_platform_v3.build_manifest import (
    LEHRER_BUILD_MANIFEST_PATH,
    SUPPORTED_BUILD_MANIFEST_VERSIONS,
    BuildManifestMismatchError,
    check_build_manifest,
)
from ol_concourse.pipelines.open_edx.edx_platform_v3.meta import (
    lehrer_build_manifest,
    meta_pipeline,
)
from ol_concourse.pipelines.open_edx.edx_platform_v3.pipeline import (
    build_edx_pipeline,
)

RELEASES = [release.name for release in OpenEdxSupportedRelease]
TOPOLOGY = sorted(
    (release, deployment.deployment_name)
    for release in RELEASES
    for deployment in filter_deployments_by_release(release)
)
VERSION = min(SUPPORTED_BUILD_MANIFEST_VERSIONS)


def _manifest(
    tmp_path: Path, cells: list[tuple[str, str]], **top_level: object
) -> Path:
    path = tmp_path / "build_manifest.yaml"
    document = {
        "version": VERSION,
        **top_level,
        "cells": [
            {"release": release, "deployment": deployment, "packages": ["a==1"]}
            for release, deployment in cells
        ],
    }
    path.write_text(yaml.safe_dump(document))
    return path


def test_topology_is_not_empty() -> None:
    """Every other test here is vacuous if the topology yields no coordinates."""
    assert TOPOLOGY


def test_pipeline_builds_from_a_manifest_with_every_cell(tmp_path: Path) -> None:
    pipeline = build_edx_pipeline(RELEASES, _manifest(tmp_path, TOPOLOGY))
    assert pipeline.jobs


def test_extra_cells_are_allowed(tmp_path: Path) -> None:
    """Lehrer may build cells this topology doesn't deploy yet."""
    manifest = _manifest(tmp_path, [*TOPOLOGY, ("future", "nowhere")])
    check_build_manifest(manifest, TOPOLOGY)


def test_missing_cell_fails_set_pipeline(tmp_path: Path) -> None:
    dropped = TOPOLOGY[0]
    manifest = _manifest(tmp_path, TOPOLOGY[1:])
    with pytest.raises(BuildManifestMismatchError, match="/".join(dropped)):
        build_edx_pipeline(RELEASES, manifest)


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(max(SUPPORTED_BUILD_MANIFEST_VERSIONS) + 1, id="newer"),
        pytest.param(0, id="older"),
        pytest.param(str(VERSION), id="string"),
        # True == 1, so a bare membership test would accept it.
        pytest.param(True, id="bool"),
    ],
)
def test_unsupported_schema_version_fails_set_pipeline(
    tmp_path: Path, version: object
) -> None:
    manifest = _manifest(tmp_path, TOPOLOGY, version=version)
    with pytest.raises(BuildManifestMismatchError, match="schema version"):
        build_edx_pipeline(RELEASES, manifest)


def test_missing_schema_version_fails(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, TOPOLOGY)
    document = yaml.safe_load(manifest.read_text())
    del document["version"]
    manifest.write_text(yaml.safe_dump(document))
    with pytest.raises(BuildManifestMismatchError, match="schema version None"):
        check_build_manifest(manifest, TOPOLOGY)


def test_meta_hands_the_generator_the_manifest_the_build_reads() -> None:
    """The checked file is the one the build job passes to dagger."""
    (global_job,) = [job for job in meta_pipeline.jobs if "global" in str(job.name)]
    plan = global_job.model_dump(exclude_none=True)["plan"]
    gets = {step["get"]: step for step in plan if "get" in step}
    assert gets[str(lehrer_build_manifest.name)]["trigger"] is True
    (task,) = [step for step in plan if "task" in step]
    assert {"name": str(lehrer_build_manifest.name)} in task["config"]["inputs"]
    assert task["config"]["params"]["LEHRER_BUILD_MANIFEST"] == (
        f"../{lehrer_build_manifest.name}/{LEHRER_BUILD_MANIFEST_PATH}"
    )
    assert [str(path) for path in lehrer_build_manifest.source["paths"]] == [
        LEHRER_BUILD_MANIFEST_PATH
    ]


def test_build_job_reads_the_checked_manifest(tmp_path: Path) -> None:
    pipeline = build_edx_pipeline(RELEASES, _manifest(tmp_path, TOPOLOGY))
    build_jobs = [job for job in pipeline.jobs if str(job.name).startswith("build-")]
    assert build_jobs
    for job in build_jobs:
        assert f"--build-manifest ./{LEHRER_BUILD_MANIFEST_PATH} " in (
            job.model_dump_json()
        )
