"""Guard the version-pin projection and `versions_map` against drift.

Two things have to stay true for narrowing the pipelines' watched paths to be
safe rather than merely quieter:

1. ``src/bridge/lib/version_pins/`` is an exact projection of ``versions.py``.
   A pin that stops moving when its constant moves is a pipeline that stops
   deploying, silently.
2. Every project and image watches every constant it actually reads.  Watching
   too little is a much worse failure than the over-triggering this registry
   exists to fix, so these tests re-derive the truth from the source tree and
   compare.
"""

import ast
from pathlib import Path

import pytest

from bridge.lib.sync_version_pins import PINS_README, parse_pins
from ol_concourse.pipelines.versions_map import (
    IMAGE_VERSIONS,
    PROJECT_VERSIONS,
    VERSION_PINS_ROOT,
    image_version_paths,
    project_version_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PULUMI_SRC = SRC / "ol_infrastructure"
BILDER_IMAGES = SRC / "bilder" / "images"
VERSIONS_FILE = SRC / "bridge" / "lib" / "versions.py"
PINS_DIR = REPO_ROOT / VERSION_PINS_ROOT
VERSIONS_MODULE = "bridge.lib.versions"


def _module_file(module: str) -> Path | None:
    """Resolve a dotted module name to a file under ``src/``, if it is local."""
    candidate = SRC / (module.replace(".", "/") + ".py")
    if candidate.exists():
        return candidate
    package = SRC / module.replace(".", "/") / "__init__.py"
    return package if package.exists() else None


def _imported_module_files(node: ast.AST) -> set[Path]:
    """Resolve an import node to the local files under ``src/`` it pulls in."""
    if isinstance(node, ast.ImportFrom) and node.module:
        candidates = (
            node.module,
            *(f"{node.module}.{alias.name}" for alias in node.names),
        )
    elif isinstance(node, ast.Import):
        candidates = tuple(alias.name for alias in node.names)
    else:
        return set()
    return {
        resolved
        for resolved in (_module_file(candidate) for candidate in candidates)
        if resolved
    }


def _versions_module_aliases(node: ast.AST) -> set[str]:
    """Names bound to the whole ``bridge.lib.versions`` module by an import."""
    if isinstance(node, ast.ImportFrom) and node.module == "bridge.lib":
        return {
            alias.asname or alias.name
            for alias in node.names
            if alias.name == "versions"
        }
    if isinstance(node, ast.Import):
        return {
            alias.asname or "versions"
            for alias in node.names
            if alias.name == VERSIONS_MODULE
        }
    return set()


def _analyze(path: Path) -> tuple[set[str], set[Path]]:
    """Return (constants read directly by ``path``, local modules it imports)."""
    tree = ast.parse(path.read_text())

    constants: set[str] = set()
    imports: set[Path] = set()
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == VERSIONS_MODULE:
            constants.update(alias.name for alias in node.names)
        aliases |= _versions_module_aliases(node)
        imports |= _imported_module_files(node)

    # `from bridge.lib import versions` + `versions.FOO` is rare but legal, and
    # missing it would be a false negative -- the direction that hurts.
    if aliases:
        constants.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        )
    return constants, imports


_ANALYSIS_CACHE: dict[Path, tuple[set[str], set[Path]]] = {}


def _cached_analyze(path: Path) -> tuple[set[str], set[Path]]:
    """Memoized :func:`_analyze`, tolerating files this Python cannot parse."""
    if path not in _ANALYSIS_CACHE:
        try:
            _ANALYSIS_CACHE[path] = _analyze(path)
        except SyntaxError:
            _ANALYSIS_CACHE[path] = (set(), set())
    return _ANALYSIS_CACHE[path]


def _transitive_versions(path: Path, seen: set[Path] | None = None) -> set[str]:
    """Collect the constants read by ``path`` or by anything it imports."""
    seen = seen if seen is not None else set()
    if path in seen:
        return set()
    seen.add(path)
    direct, imports = _cached_analyze(path)
    return direct.union(*(_transitive_versions(i, seen) for i in imports), set())


def _actual_versions(root: Path) -> set[str]:
    """Collect every version constant read anywhere under a directory."""
    return set().union(*(_transitive_versions(f) for f in root.rglob("*.py")), set())


def _pulumi_projects() -> list[str]:
    """List every Pulumi project path, relative to ``src/ol_infrastructure/``."""
    return sorted(
        str(p.parent.relative_to(PULUMI_SRC)) + "/"
        for p in PULUMI_SRC.rglob("Pulumi.yaml")
    )


def _packer_images() -> list[str]:
    """List every Packer image directory name under ``src/bilder/images/``."""
    return sorted(
        d.name
        for d in BILDER_IMAGES.iterdir()
        if d.is_dir() and not d.name.startswith("__")
    )


def test_pins_match_versions_file() -> None:
    """version_pins/ is an exact projection of versions.py."""
    expected = {
        name: f"{value}\n"
        for name, value in parse_pins(VERSIONS_FILE.read_text()).items()
    }
    actual = {
        pin.name: pin.read_text()
        for pin in PINS_DIR.iterdir()
        if pin.name != PINS_README
    }
    assert actual == expected, (
        "src/bridge/lib/version_pins/ is out of sync with versions.py. Run "
        "`python src/bridge/lib/sync_version_pins.py` (pre-commit does this "
        "for you). Until it is back in sync, pipelines watching a stale pin "
        "will not redeploy when that version moves."
    )


@pytest.mark.parametrize("project", _pulumi_projects())
def test_every_pulumi_project_is_registered(project: str) -> None:
    """Every Pulumi project on disk has an audited PROJECT_VERSIONS entry."""
    assert project in PROJECT_VERSIONS, (
        f"Pulumi project {project!r} has no entry in PROJECT_VERSIONS. Add one "
        "(an empty list is correct if it reads no version constants), otherwise "
        "its pipeline will not re-trigger when a version it pins changes."
    )


@pytest.mark.parametrize("image", _packer_images())
def test_every_packer_image_is_registered(image: str) -> None:
    """Every Packer image on disk has an audited IMAGE_VERSIONS entry."""
    assert image in IMAGE_VERSIONS, (
        f"Packer image {image!r} has no entry in IMAGE_VERSIONS. Add one (an "
        "empty list is correct if it bakes in no pinned version), otherwise its "
        "AMI will not rebuild when a version it installs changes."
    )


@pytest.mark.parametrize("project", _pulumi_projects())
def test_registry_covers_every_version_the_project_reads(project: str) -> None:
    """A project's pipeline watches every version constant it reads."""
    watched = set(project_version_paths(project))
    missing = sorted(
        name
        for name in _actual_versions(PULUMI_SRC / project)
        if f"{VERSION_PINS_ROOT}/{name}" not in watched
    )
    assert not missing, (
        f"{project} reads {missing} but its pipeline does not watch them. Add "
        "the entries to PROJECT_VERSIONS in "
        "src/ol_concourse/pipelines/versions_map.py."
    )


@pytest.mark.parametrize("image", _packer_images())
def test_registry_covers_every_version_the_image_reads(image: str) -> None:
    """An image's pipeline watches every version constant it bakes in."""
    watched = set(image_version_paths(image))
    missing = sorted(
        name
        for name in _actual_versions(BILDER_IMAGES / image)
        if f"{VERSION_PINS_ROOT}/{name}" not in watched
    )
    assert not missing, (
        f"bilder image {image} installs {missing} but its pipeline does not "
        "watch them. Add the entries to IMAGE_VERSIONS in "
        "src/ol_concourse/pipelines/versions_map.py."
    )


def _paths_lists(tree: ast.Module) -> list[ast.List]:
    """Every ``paths=[...]`` literal passed to a call in a pipeline module."""
    return [
        keyword.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "paths" and isinstance(keyword.value, ast.List)
    ]


def _helper_args(paths_list: ast.List, helper: str) -> set[str]:
    """Arguments a helper is starred into a ``paths=[...]`` list with."""
    return {
        ast.unparse(element.value.args[0])
        for element in paths_list.elts
        if isinstance(element, ast.Starred)
        and isinstance(element.value, ast.Call)
        and isinstance(element.value.func, ast.Name)
        and element.value.func.id == helper
        and element.value.args
    }


def _pipeline_modules() -> list[str]:
    """Every pipeline definition module under ``src/ol_concourse/pipelines/``."""
    root = SRC / "ol_concourse" / "pipelines"
    return sorted(str(p.relative_to(REPO_ROOT)) for p in root.rglob("pipeline.py"))


@pytest.mark.parametrize("module", _pipeline_modules())
def test_watched_secrets_and_version_pins_stay_paired(module: str) -> None:
    """A git resource watching a project's secrets also watches its pins.

    The registry alone does not narrow anything -- a pipeline has to call
    ``project_version_paths``.  Bespoke pipelines (JupyterHub, Superset,
    Kubewatch, the Open edX and k8s_apps factories) each build their own
    ``paths`` list, so it is easy to add one helper and forget the other and
    quietly ship a pipeline that never redeploys on a version bump.  Every
    Pulumi git resource already watches its project's secrets, so pairing the
    two calls is the invariant that keeps the guarantee true.
    """
    tree = ast.parse((REPO_ROOT / module).read_text())
    unpaired = sorted(
        f"{secrets_helper}({arg})"
        for paths_list in _paths_lists(tree)
        for secrets_helper, versions_helper in (
            ("project_secrets_paths", "project_version_paths"),
            ("combined_secrets_paths", "combined_version_paths"),
        )
        for arg in _helper_args(paths_list, secrets_helper)
        - _helper_args(paths_list, versions_helper)
    )
    assert not unpaired, (
        f"{module} watches secrets without watching version pins: {unpaired}. "
        "Add the matching project_version_paths(...)/combined_version_paths(...) "
        "call to the same paths list, otherwise that resource will not "
        "re-trigger when a version the project pins changes."
    )


def test_registry_has_no_stale_entries() -> None:
    """The registry does not list projects or images that no longer exist."""
    stale = sorted(
        [
            *(set(PROJECT_VERSIONS) - set(_pulumi_projects())),
            *(set(IMAGE_VERSIONS) - set(_packer_images())),
        ]
    )
    assert not stale, (
        f"versions_map lists things that no longer exist: {stale}. Remove them "
        "so the registry keeps matching the tree."
    )


def test_registry_entries_name_real_constants() -> None:
    """Every registered constant exists in versions.py (and so has a pin file)."""
    known = set(parse_pins(VERSIONS_FILE.read_text()))
    unknown = sorted(
        f"{owner} -> {name}"
        for registry in (PROJECT_VERSIONS, IMAGE_VERSIONS)
        for owner, names in registry.items()
        for name in names
        if name not in known
    )
    assert not unknown, (
        f"versions_map names constants that are not in versions.py: {unknown}. "
        "A pipeline watching a nonexistent pin file simply never triggers on it."
    )
