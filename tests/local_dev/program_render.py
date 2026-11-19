"""Render a local-dev app's Pulumi program under mocks.

Shared by the per-app program tests (test_mitxonline_program.py,
test_learn_ai_program.py): each pins what its program must match deployed, on
the resources this harness records.
"""

import asyncio
import json
import runpy
import shutil
from pathlib import Path
from typing import Any

import pulumi
import pytest

# Python 3.14+ compatibility, as
# tests/ol_infrastructure/applications/mitxonline/test_definition.py does.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

REPO = Path(__file__).parents[2]
DEPLOYMENT = "kubernetes:apps/v1:Deployment"
SERVICE = "kubernetes:core/v1:Service"
JOB = "kubernetes:batch/v1:Job"
SECRET_TYPE = "kubernetes:core/v1:Secret"  # noqa: S105  # pragma: allowlist secret
ROUTE = "kubernetes:apisix.apache.org/v2:ApisixRoute"
PLUGIN_CONFIG = "kubernetes:apisix.apache.org/v2:ApisixPluginConfig"
OIDC_SECRET = "ol-mitlearn-oidc"  # noqa: S105  # pragma: allowlist secret
APP_COMPONENT = "ol:infrastructure:components:services:OLApplicationK8s"


class ProgramMocks(pulumi.runtime.Mocks):
    """Record every registered resource, and the program's namespace after the run."""

    def __init__(self) -> None:
        self.resources: list[tuple[str, str, dict[str, Any]]] = []
        self.transforms: dict[str, list[Any]] = {}
        self.program: dict[str, Any] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append((args.typ, args.name, args.inputs))
        if args.transforms:
            self.transforms[args.typ] = list(args.transforms)
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _tree(app: str, tmp_path: Path, app_files: dict[str, str] | None) -> Path:
    """Copy the app dir with pulumi/ inside; the program takes its parent as APP_DIR."""
    root = tmp_path / "local-dev"
    shutil.copytree(
        REPO / "local-dev/apps" / app,
        root / "apps" / app,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for relpath, text in (app_files or {}).items():
        target = root / "apps" / app / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root / "apps" / app / "pulumi"


def render_program(  # noqa: PLR0913
    app: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root_domain: str = "mit.test",
    config: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    app_files: dict[str, str] | None = None,
) -> ProgramMocks:
    """Run local-dev/apps/<app>/pulumi under mocks and return what it registered.

    ``config`` is the stack file's config: block; ``app_files`` overrides files
    in the copy of the app dir the program reads.
    """
    for key in (
        "TILT_IMAGE_0",
        "LOCAL_DEV_PREBUILT_IMAGE_TAGS",
        "LOCAL_DEV_ANALYTICS_API_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCAL_DEV_ROOT_DOMAIN", root_domain)
    for key, value in (environ or {}).items():
        monkeypatch.setenv(key, value)
    mocks = ProgramMocks()
    # Set per render, not at import: other test modules set their own mocks at
    # import time and the last one wins.
    pulumi.runtime.set_mocks(
        mocks,
        project=f"ol-local-dev-{app}",
        stack="Dev",
        preview=False,
    )
    # An object value arrives JSON-encoded, as the CLI hands it to the program.
    pulumi.runtime.set_all_config(
        {
            key: value if isinstance(value, str) else json.dumps(value)
            for key, value in (config or {}).items()
        }
    )
    program_dir = _tree(app, tmp_path, app_files)

    @pulumi.runtime.test
    def run():
        mocks.program = runpy.run_path(str(program_dir), run_name="__main__")
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return mocks


def one(mocks: ProgramMocks, typ: str, name: str) -> dict[str, Any]:
    """Return the inputs of the one resource of type ``typ`` named ``name``."""
    matches = [
        i
        for t, _, i in mocks.resources
        if t == typ and i.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"{typ} {name}: {len(matches)} found"
    return matches[0]
