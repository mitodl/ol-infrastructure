"""Helpers shared by the tests that build the edxapp pipeline from a lehrer manifest."""

from pathlib import Path

import yaml

from bridge.settings.openedx.accessors import filter_deployments_by_release
from bridge.settings.openedx.types import OpenEdxSupportedRelease
from ol_concourse.pipelines.open_edx.edx_platform_v3.build_manifest import (
    SUPPORTED_BUILD_MANIFEST_VERSIONS,
)

RELEASES = [release.name for release in OpenEdxSupportedRelease]
TOPOLOGY = sorted(
    (release, deployment.deployment_name)
    for release in RELEASES
    for deployment in filter_deployments_by_release(release)
)
VERSION = min(SUPPORTED_BUILD_MANIFEST_VERSIONS)


def write_manifest(
    directory: Path, cells: list[tuple[str, str]], **top_level: object
) -> Path:
    """Write a build_manifest.yaml with ``cells`` into ``directory``."""
    path = directory / "build_manifest.yaml"
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
