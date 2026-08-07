"""Tests for the Sentry configuration importer."""

from pathlib import Path
from runpy import run_path
from typing import Any

import pytest

IMPORTER = run_path(
    str(Path(__file__).resolve().parents[1].joinpath("bin/import-sentry-config"))
)
build_program = IMPORTER["build_program"]


def inventory_with_project(project: dict[str, Any]) -> dict[str, Any]:
    """Return the smallest inventory accepted by build_program."""
    return {
        "organization": {"name": "Test Organization", "slug": "test-org"},
        "teams": [],
        "projects": [project],
        "members": [],
        "repositories": [],
        "code_mappings": [],
        "dashboards": [],
        "keys": [],
        "issue_alerts": [],
        "metric_alerts": [],
        "plugins": [],
    }


def test_mapped_project_name_renders_as_services_expression() -> None:
    """Mapped names use ol_types while the live Sentry slug stays unchanged."""
    program = build_program(
        inventory_with_project(
            {
                "name": "ODL Video Service",
                "slug": "odl-video-service",
                "platform": "python",
            }
        ),
        "test-org",
    )

    project_block = program.blocks[1]
    assert "name=Services.odl_video_service," in project_block
    assert "slug='odl-video-service'," in project_block
    assert "name='ODL Video Service'," not in project_block


def test_release_script_keeps_literal_name_as_phase_out_exception() -> None:
    """The retiring release-script project does not require a Services member."""
    program = build_program(
        inventory_with_project(
            {
                "name": "release-script",
                "slug": "release-script",
                "platform": "python",
            }
        ),
        "test-org",
    )

    project_block = program.blocks[1]
    assert "name='release-script'," in project_block
    assert "slug='release-script'," in project_block
    assert any("being phased out" in warning for warning in program.warnings)


def test_unknown_project_fails_generation() -> None:
    """New Sentry projects require an explicit ontology decision."""
    with pytest.raises(ValueError, match="no Services mapping or documented"):
        build_program(
            inventory_with_project(
                {
                    "name": "Unmapped Service",
                    "slug": "unmapped-service",
                    "platform": "python",
                }
            ),
            "test-org",
        )
