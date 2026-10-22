"""Tests for the check that keeps the canaries' ``docs/journeys.md`` current."""

from pathlib import Path

import pytest

from ol_concourse.pipelines.canaries.check_journeys_doc import (
    count_tests,
    find_problems,
)

SPEC = """\
import { expect, test } from "@playwright/test"

test.describe("A property", () => {
  test("first journey", async ({ page }) => {
    await test.step("a step", async () => {})
  })
  test.skip("a skipped journey", async () => {})
})
"""

JOURNEYS = """\
# Canary journeys, step by step

## prop

Two tests in one spec file.

`specs/prop/journey.spec.ts`: anonymous.

![The homepage](images/prop/journey/01-homepage.jpg)

## What a failure looks like
"""

CAPTURE = """\
test("journey", async ({ page }) => {
  await shot(page, "journey/01-homepage")
})
"""


@pytest.fixture
def canaries(tmp_path: Path) -> Path:
    """Build a minimal canaries tree that the check accepts."""
    (tmp_path / "specs" / "prop" / "helpers").mkdir(parents=True)
    (tmp_path / "specs" / "prop" / "journey.spec.ts").write_text(SPEC)
    (tmp_path / "specs" / "prop" / "helpers" / "fixtures.ts").write_text("")
    (tmp_path / "docs" / "capture").mkdir(parents=True)
    (tmp_path / "docs" / "capture" / "prop.capture.ts").write_text(CAPTURE)
    image = tmp_path / "docs" / "images" / "prop" / "journey" / "01-homepage.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"")
    (tmp_path / "docs" / "journeys.md").write_text(JOURNEYS)
    return tmp_path


def _rewrite_journeys(canaries: Path, old: str, new: str) -> None:
    journeys = canaries / "docs" / "journeys.md"
    text = journeys.read_text()
    assert old in text
    journeys.write_text(text.replace(old, new))


def test_the_committed_page_matches_the_specs() -> None:
    assert find_problems() == []


def test_a_consistent_tree_passes(canaries: Path) -> None:
    assert find_problems(canaries) == []


def test_count_tests_counts_declarations_only() -> None:
    commented = '// test("an old journey", async () => {})\n'
    assert count_tests(SPEC) == 2
    assert count_tests(commented + SPEC) == 2


def test_count_sentence_accepts_digits(canaries: Path) -> None:
    _rewrite_journeys(
        canaries, "Two tests in one spec file.", "2 tests in 1 spec file."
    )
    assert find_problems(canaries) == []


def test_a_property_without_a_section_is_reported(canaries: Path) -> None:
    (canaries / "specs" / "other").mkdir()
    (canaries / "specs" / "other" / "a.spec.ts").write_text(SPEC)
    assert find_problems(canaries) == ["journeys.md has no '## other' section."]


def test_a_directory_without_specs_is_not_a_property(canaries: Path) -> None:
    (canaries / "specs" / "scratch").mkdir()
    (canaries / "specs" / "scratch" / "notes.md").write_text("")
    assert find_problems(canaries) == []


def test_an_unnamed_spec_and_a_stale_count_are_reported(canaries: Path) -> None:
    (canaries / "specs" / "prop" / "tracking.spec.ts").write_text(
        'test("loads the tag", async () => {})\n'
    )
    assert find_problems(canaries) == [
        "The '## prop' section never names specs/prop/tracking.spec.ts.",
        "The '## prop' section says 'Two tests in one spec file'. "
        "On disk: 3 tests in 2 spec files.",
    ]


def test_a_missing_count_sentence_is_reported(canaries: Path) -> None:
    _rewrite_journeys(canaries, "Two tests in one spec file.", "")
    assert find_problems(canaries) == [
        "The '## prop' section has no 'N tests in M spec files' sentence. "
        "On disk: 2 tests in 1 spec files."
    ]


def test_a_missing_capture_script_is_reported(canaries: Path) -> None:
    (canaries / "docs" / "capture" / "prop.capture.ts").unlink()
    assert find_problems(canaries) == [
        "docs/capture/prop.capture.ts does not exist, so the prop pictures "
        "cannot be regenerated."
    ]


def test_a_renamed_frame_is_reported_from_both_ends(canaries: Path) -> None:
    _rewrite_journeys(canaries, "01-homepage.jpg", "01-home.jpg")
    assert find_problems(canaries) == [
        "prop.capture.ts writes images/prop/journey/01-homepage.jpg, but "
        "journeys.md never shows it.",
        "journeys.md shows images/prop/journey/01-home.jpg, which does not exist.",
        "images/prop/journey/01-homepage.jpg is committed but journeys.md never "
        "shows it.",
    ]


def test_every_playwright_spec_extension_is_a_spec(canaries: Path) -> None:
    (canaries / "specs" / "prop" / "catalog.spec.tsx").write_text(
        'test("lists the catalog", async () => {})\n'
    )
    assert find_problems(canaries) == [
        "The '## prop' section never names specs/prop/catalog.spec.tsx.",
        "The '## prop' section says 'Two tests in one spec file'. "
        "On disk: 3 tests in 2 spec files.",
    ]


def test_non_image_files_under_images_are_ignored(canaries: Path) -> None:
    (canaries / "docs" / "images" / "prop" / ".DS_Store").write_bytes(b"")
    assert find_problems(canaries) == []
