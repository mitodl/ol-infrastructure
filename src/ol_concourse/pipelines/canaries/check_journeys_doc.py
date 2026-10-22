"""Fail when ``docs/journeys.md`` has fallen out of step with the canary specs.

``journeys.md`` is the page a human reads to learn what each canary does, and
``AGENTS.md`` asks that a new or changed journey update it in the same PR. That
rule is easy to miss: the HubSpot journey (#5823) merged with no section, and
the page went on saying "six tests in four spec files" for 27 days. This is the
mechanical half of that rule, run by the ``check-canary-journeys-doc`` hook.

For every property, meaning every directory under ``specs/`` holding a spec, it
checks that:

- ``journeys.md`` has a ``## <property>`` section, and
  ``docs/capture/<property>.capture.ts`` exists to regenerate its pictures;
- the section names every spec file, and its "N tests in M spec files" sentence
  matches what is on disk;
- every frame the capture script writes under a fixed name is shown on the page.

And across the page, that every image it shows exists and every image under
``docs/images/`` is shown, so a renamed frame cannot leave a broken picture or
an orphaned binary behind.

What it cannot check is the prose: whether a step still describes what its spec
asserts, or whether the capture still mirrors the spec. Those stay with review.

Parsed with regular expressions rather than by asking Playwright, so that this
runs as a hook with nothing but the standard library: ``playwright test --list``
needs ``npm ci`` and a ``CANARY_BASE_URL``, neither of which a hook should.
"""

import re
import sys
from pathlib import Path

CANARIES_DIR = Path(__file__).parent

# Playwright's default testMatch, `**/*.@(spec|test).?(c|m)[jt]s?(x)`, which
# playwright.config.ts does not override.
SPEC_FILE = re.compile(r"\.(?:spec|test)\.[cm]?[jt]sx?$")
# What counts as a picture for the orphan check. Anything else under images/,
# such as a .DS_Store left by opening the folder, is not the page's business.
IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})

# A test declaration, skipped or not: `test("...", ...)`, `test.skip("...", ...)`.
# Not `test.describe(`, `test.step(` or `test.extend(`, and not a conditional
# `test.skip(condition)` inside a test body, whose first argument is no title.
TEST_DECLARATION = re.compile(
    r"(?<![\w.])test(?:\.(?:skip|fixme|fail|slow))?\(\s*[\"'`]"
)
COMMENT_LINE = re.compile(r"^\s*(?://|/?\*)")
COUNT_SENTENCE = re.compile(
    r"\b(\w+) tests? in (\w+) spec files?\b", flags=re.IGNORECASE
)
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")
# Only literal frame names; a templated one is covered by the orphan check
# once its frames are committed.
CAPTURE_SHOT = re.compile(r"\bshot\(\s*page,\s*\"([^\"]+)\"")

NUMBER_WORDS = {
    word: value
    for value, word in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ]
    )
}


def parse_number(text: str) -> int | None:
    """Read a count written as digits or as a word up to twenty."""
    if text.isdigit():
        return int(text)
    return NUMBER_WORDS.get(text.lower())


def count_tests(source: str) -> int:
    """Count the test declarations in a spec, ignoring commented-out lines.

    Only declarations with a literal title count. A test declared in a loop,
    with its title in a variable, or through a fixture renamed away from
    ``test`` is counted wrongly; none of the specs do that today, and one that
    starts to will see its count disagree with ``playwright test --list``.
    """
    code = "\n".join(
        line for line in source.splitlines() if not COMMENT_LINE.match(line)
    )
    return len(TEST_DECLARATION.findall(code))


def spec_files(property_dir: Path) -> list[Path]:
    """Every file Playwright would load as a spec under one property."""
    return sorted(
        path
        for path in property_dir.rglob("*")
        if path.is_file() and SPEC_FILE.search(path.name)
    )


def sections(document: str) -> dict[str, str]:
    """Split the page into its ``## `` sections, keyed by heading text."""
    found: dict[str, str] = {}
    for chunk in re.split(r"^## ", document, flags=re.MULTILINE)[1:]:
        heading, _, body = chunk.partition("\n")
        found[heading.strip()] = body
    return found


def _count_problems(name: str, section: str, specs: list[Path]) -> list[str]:
    """Compare the section's "N tests in M spec files" sentence with the specs."""
    total_tests = sum(count_tests(spec.read_text()) for spec in specs)
    on_disk = f"On disk: {total_tests} tests in {len(specs)} spec files."
    sentence = COUNT_SENTENCE.search(section)
    if sentence is None:
        return [
            f"The '## {name}' section has no 'N tests in M spec files' sentence. "
            + on_disk
        ]
    stated = (parse_number(sentence.group(1)), parse_number(sentence.group(2)))
    if stated != (total_tests, len(specs)):
        return [f"The '## {name}' section says '{sentence.group(0)}'. " + on_disk]
    return []


def _property_problems(
    canaries_dir: Path, property_dir: Path, document: str, section: str
) -> list[str]:
    """Check one property's section, specs, and capture script."""
    name = property_dir.name
    specs = spec_files(property_dir)
    problems = [
        f"The '## {name}' section never names {relative}."
        for relative in (spec.relative_to(canaries_dir).as_posix() for spec in specs)
        if relative not in section
    ]
    problems += _count_problems(name, section, specs)

    capture = canaries_dir / "docs" / "capture" / f"{name}.capture.ts"
    if not capture.exists():
        problems.append(
            f"{capture.relative_to(canaries_dir)} does not exist, so the "
            f"{name} pictures cannot be regenerated."
        )
        return problems
    problems += [
        f"{capture.name} writes {image}, but journeys.md never shows it."
        for image in (
            f"images/{name}/{frame}.jpg"
            for frame in CAPTURE_SHOT.findall(capture.read_text())
        )
        if f"]({image})" not in document
    ]
    return problems


def _image_problems(docs_dir: Path, document: str) -> list[str]:
    """Every image the page shows exists, and every committed image is shown."""
    linked = set(IMAGE_LINK.findall(document))
    problems = [
        f"journeys.md shows {image}, which does not exist."
        for image in sorted(linked)
        if not (docs_dir / image).exists()
    ]
    images_dir = docs_dir / "images"
    if images_dir.exists():
        problems += [
            f"{image} is committed but journeys.md never shows it."
            for image in (
                path.relative_to(docs_dir).as_posix()
                for path in sorted(images_dir.rglob("*"))
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
            if image not in linked
        ]
    return problems


def find_problems(canaries_dir: Path = CANARIES_DIR) -> list[str]:
    """Every way ``docs/journeys.md`` disagrees with the tree it documents."""
    docs_dir = canaries_dir / "docs"
    journeys = docs_dir / "journeys.md"
    if not journeys.exists():
        return [f"{journeys.relative_to(canaries_dir)} does not exist."]
    document = journeys.read_text()
    page_sections = sections(document)

    problems: list[str] = []
    specs_dir = canaries_dir / "specs"
    for property_dir in sorted(specs_dir.iterdir()):
        if not (property_dir.is_dir() and spec_files(property_dir)):
            continue
        section = page_sections.get(property_dir.name)
        if section is None:
            problems.append(f"journeys.md has no '## {property_dir.name}' section.")
            continue
        problems += _property_problems(canaries_dir, property_dir, document, section)
    return problems + _image_problems(docs_dir, document)


def main() -> int:
    problems = find_problems()
    for problem in problems:
        print(problem, file=sys.stderr)  # noqa: T201
    if problems:
        print(  # noqa: T201
            "\nUpdate docs/journeys.md in the same PR as the journey. See its "
            "'Keeping this page current' section.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
