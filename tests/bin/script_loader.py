"""Import a `bin/` script as a module so its functions can be tested."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BIN_DIR = Path(__file__).resolve().parents[2] / "bin"


def load_script(script_name: str) -> ModuleType:
    """Load ``bin/<script_name>``; `bin/` is not a package and the files have no suffix.

    :param script_name: The script's file name, e.g. ``"promotion-gate-age"``.
    :returns: The executed module, registered in ``sys.modules``.
    :rtype: ModuleType
    """
    module_name = "test_bin_" + script_name.replace("-", "_")
    script_path = BIN_DIR / script_name
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    if spec is None or spec.loader is None:
        msg = f"Unable to load module from {script_path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
