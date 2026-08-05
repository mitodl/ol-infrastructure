"""Guard `ol_concourse.pipelines.secrets_map` against drift.

The registry decides which Concourse pipelines re-trigger when a SOPS secret
changes.  If a Pulumi project starts reading a new secret and nobody updates the
registry, that project's pipeline silently stops noticing the change -- a much
worse failure than the over-triggering the registry exists to fix.  These tests
re-derive the truth from the source tree and compare.
"""

import ast
from pathlib import Path

import pytest

from ol_concourse.pipelines.secrets_map import (
    CONTENT_BEARING_OVERRIDES,
    DEPLOY_CREDENTIAL_SECRETS,
    DYNAMIC_SECRET_READS,
    PROJECT_SECRETS,
    SECRETS_ROOT,
    project_secrets_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PULUMI_SRC = SRC / "ol_infrastructure"
SECRET_READERS = frozenset(
    {"read_yaml_secrets", "read_json_secrets", "set_env_secrets"}
)


def _module_file(module: str) -> Path | None:
    """Resolve a dotted module name to a file under ``src/``, if it is local."""
    candidate = SRC / (module.replace(".", "/") + ".py")
    if candidate.exists():
        return candidate
    package = SRC / module.replace(".", "/") / "__init__.py"
    return package if package.exists() else None


def _as_path_string(node: ast.AST) -> str | None:
    """Render a ``Path(...)``-ish expression, with f-string holes as ``*``."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "*"
            for part in node.values
        )
    if isinstance(node, ast.Call):
        parts = [_as_path_string(arg) for arg in node.args]
        return None if any(part is None for part in parts) else "/".join(parts)  # type: ignore[arg-type]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _as_path_string(node.left), _as_path_string(node.right)
        return f"{left}/{right}" if left and right else right
    return None


def _path_valued_assignments(tree: ast.Module) -> dict[str, str]:
    """Map module-level names to the path expression they were assigned."""
    assigned: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            rendered = _as_path_string(node.value)
            if rendered:
                assigned[node.targets[0].id] = rendered
    return assigned


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


def _analyze(path: Path) -> tuple[set[str], set[Path]]:
    """Return (secret paths read directly by ``path``, local modules it imports)."""
    tree = ast.parse(path.read_text())
    local_paths = _path_valued_assignments(tree)

    secrets: set[str] = set()
    imports: set[Path] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in SECRET_READERS
            and node.args
        ):
            arg = node.args[0]
            rendered = _as_path_string(arg)
            if rendered is None and isinstance(arg, ast.Name):
                rendered = local_paths.get(arg.id)
            if rendered:
                secrets.add(rendered)
        else:
            imports |= _imported_module_files(node)
    return secrets, imports


_ANALYSIS_CACHE: dict[Path, tuple[set[str], set[Path]]] = {}


def _cached_analyze(path: Path) -> tuple[set[str], set[Path]]:
    """Memoized :func:`_analyze`, tolerating files this Python cannot parse."""
    if path not in _ANALYSIS_CACHE:
        try:
            _ANALYSIS_CACHE[path] = _analyze(path)
        except SyntaxError:
            _ANALYSIS_CACHE[path] = (set(), set())
    return _ANALYSIS_CACHE[path]


def _transitive_secrets(path: Path, seen: set[Path] | None = None) -> set[str]:
    """Collect the secrets read by ``path`` or by anything it imports."""
    seen = seen if seen is not None else set()
    if path in seen:
        return set()
    seen.add(path)
    direct, imports = _cached_analyze(path)
    return direct.union(*(_transitive_secrets(i, seen) for i in imports), set())


def _actual_secrets(project: Path) -> set[str]:
    """Collect every secret read anywhere under a Pulumi project directory."""
    return set().union(*(_transitive_secrets(f) for f in project.rglob("*.py")), set())


def _pulumi_projects() -> list[str]:
    """List every Pulumi project path, relative to ``src/ol_infrastructure/``."""
    return sorted(
        str(p.parent.relative_to(PULUMI_SRC)) + "/"
        for p in PULUMI_SRC.rglob("Pulumi.yaml")
    )


def _covers(watched: set[str], secret: str) -> bool:
    """Report whether any watched entry covers ``secret`` (relative to SECRETS_ROOT)."""
    head = secret.split("/", 1)[0]
    for entry in watched:
        rel = entry[len(SECRETS_ROOT) + 1 :]
        if rel.endswith("/"):
            if secret.startswith(rel):
                return True
        elif "/" in rel:
            # Glob within a directory, e.g. "pulumi/mongodb_atlas.*.*.yaml".
            entry_dir, entry_glob = rel.split("/", 1)
            secret_dir, _, secret_file = secret.partition("/")
            if entry_dir == secret_dir and _glob_match(entry_glob, secret_file):
                return True
        elif rel == head:
            return True
    return False


def _glob_match(pattern: str, value: str) -> bool:
    """Match ``*``-separated segments, treating ``*`` as "one path-free chunk"."""
    parts = pattern.split("*")
    if not value.startswith(parts[0]):
        return False
    cursor = len(parts[0])
    for part in parts[1:-1]:
        found = value.find(part, cursor)
        if found == -1:
            return False
        cursor = found + len(part)
    return value.endswith(parts[-1]) and cursor <= len(value) - len(parts[-1])


@pytest.mark.parametrize("project", _pulumi_projects())
def test_every_pulumi_project_is_registered(project: str) -> None:
    """Every Pulumi project on disk has an audited PROJECT_SECRETS entry."""
    assert project in PROJECT_SECRETS, (
        f"Pulumi project {project!r} has no entry in PROJECT_SECRETS. Add one "
        "(an empty list is correct if it reads no SOPS secrets), otherwise its "
        "pipeline will not re-trigger when its secrets change."
    )


def _expected_secrets(project: str) -> set[str]:
    """Derive the secrets ``project``'s pipeline must watch, after exemptions."""
    dynamic = DYNAMIC_SECRET_READS.get(project, {})
    expected: set[str] = set()
    for secret in _actual_secrets(PULUMI_SRC / project):
        if secret in dynamic:
            expected.update(dynamic[secret])
        elif (
            secret in DEPLOY_CREDENTIAL_SECRETS
            and (project, secret) not in CONTENT_BEARING_OVERRIDES
        ):
            continue
        else:
            expected.add(secret)
    return expected


@pytest.mark.parametrize("project", _pulumi_projects())
def test_registry_covers_every_secret_the_project_reads(project: str) -> None:
    """A project's pipeline watches every content-bearing secret it decrypts."""
    watched = set(project_secrets_paths(project))
    missing = sorted(
        secret for secret in _expected_secrets(project) if not _covers(watched, secret)
    )
    assert not missing, (
        f"{project} reads {missing} but its pipeline does not watch them. Add "
        "the entries to PROJECT_SECRETS in "
        "src/ol_concourse/pipelines/secrets_map.py (or, if the secret is only "
        "ever provider auth, to DEPLOY_CREDENTIAL_SECRETS with a rationale)."
    )


def test_registry_has_no_stale_projects() -> None:
    """PROJECT_SECRETS does not list Pulumi projects that no longer exist."""
    stale = sorted(set(PROJECT_SECRETS) - set(_pulumi_projects()))
    assert not stale, (
        f"PROJECT_SECRETS lists projects that no longer exist: {stale}. Remove "
        "them so the registry keeps matching the tree."
    )


def test_registry_entries_exist_on_disk() -> None:
    """Every non-glob PROJECT_SECRETS entry points at a real secrets path."""
    secrets_root = REPO_ROOT / SECRETS_ROOT
    missing = sorted(
        f"{project} -> {entry}"
        for project, entries in PROJECT_SECRETS.items()
        for entry in entries
        if "*" not in entry and not (secrets_root / entry).exists()
    )
    assert not missing, f"PROJECT_SECRETS points at nonexistent paths: {missing}"
