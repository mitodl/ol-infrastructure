"""Check lehrer's build manifest against this pipeline before it is set.

lehrer owns ``deployments/mit-ol/build_manifest.yaml``: one build cell per
``(release, deployment)``. This pipeline owns which of those cells get built and
deployed (``bridge.settings.openedx``), and refers to them only by coordinate.
Two things can break that at build time, long after ``fly set-pipeline`` said
nothing:

* lehrer drops or renames a cell this topology still builds.
* lehrer changes the manifest schema, so the coordinates this generator reads
  no longer mean what it assumes.

The second is what ``version`` is for. It names the schema the manifest is
written against (lehrer's ``SUPPORTED_MANIFEST_VERSIONS``), not a lehrer
release, so routine edits never move it. This reader declares the versions it
understands and refuses any other. Supporting a new one means checking that
``release``/``deployment`` on each entry of ``cells`` still identify a cell,
then adding the number here.
"""

from collections.abc import Iterable
from pathlib import Path

import yaml

#: Path of the mit-ol manifest inside a lehrer checkout.
LEHRER_BUILD_MANIFEST_PATH = "deployments/mit-ol/build_manifest.yaml"

#: Manifest schema versions this reader understands.
SUPPORTED_BUILD_MANIFEST_VERSIONS = frozenset({1})


class BuildManifestMismatchError(ValueError):
    """The lehrer manifest can't serve the pipeline this generator would set."""


def check_build_manifest(
    manifest_path: Path, coordinates: Iterable[tuple[str, str]]
) -> None:
    """Fail unless the manifest's schema is supported and has every coordinate.

    The manifest may hold more cells than the topology references (releases
    lehrer builds that aren't wired up here yet); only a missing one is an
    error.

    :param manifest_path: lehrer's ``build_manifest.yaml``.
    :param coordinates: ``(release, deployment)`` pairs the pipeline builds.
    :raises BuildManifestMismatchError: on a manifest that isn't a mapping, an
        unsupported or missing schema version, a missing ``cells`` list or cell
        coordinate, or a coordinate with no cell.
    """
    manifest = yaml.safe_load(manifest_path.read_text())
    if not isinstance(manifest, dict):
        msg = (
            f"{manifest_path} is not a mapping (parsed as "
            f"{type(manifest).__name__}); expected a build manifest with "
            "`version` and `cells`."
        )
        raise BuildManifestMismatchError(msg)
    version = manifest.get("version")
    # bool is an int subclass and True == 1, so `version: true` would pass a
    # bare membership test.
    if type(version) is not int or version not in SUPPORTED_BUILD_MANIFEST_VERSIONS:
        msg = (
            f"{manifest_path} declares build manifest schema version {version!r}; "
            "this pipeline reads "
            f"{sorted(SUPPORTED_BUILD_MANIFEST_VERSIONS)}. Update "
            "SUPPORTED_BUILD_MANIFEST_VERSIONS in "
            "ol_concourse/pipelines/open_edx/edx_platform_v3/build_manifest.py "
            "once this reader handles the new schema."
        )
        raise BuildManifestMismatchError(msg)
    manifest_cells = manifest.get("cells")
    if not isinstance(manifest_cells, list):
        msg = f"{manifest_path} has no `cells` list."
        raise BuildManifestMismatchError(msg)
    for index, cell in enumerate(manifest_cells):
        if absent := [
            key
            for key in ("release", "deployment")
            if not isinstance(cell, dict) or key not in cell
        ]:
            msg = f"{manifest_path} cells[{index}] lacks {' and '.join(absent)}."
            raise BuildManifestMismatchError(msg)
    cells = {(cell["release"], cell["deployment"]) for cell in manifest_cells}
    if missing := sorted(set(coordinates) - cells):
        msg = (
            f"{manifest_path} has no build cell for "
            f"{', '.join(f'{r}/{d}' for r, d in missing)}, which "
            "bridge.settings.openedx deploys. Add the cell in lehrer or drop "
            "the deployment from the topology. Cells present: "
            f"{', '.join(f'{r}/{d}' for r, d in sorted(cells))}"
        )
        raise BuildManifestMismatchError(msg)
