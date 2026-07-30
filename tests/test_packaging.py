"""What ships has to be what is here.

`dashboard.py` existed, was imported by `cli.py`, and had tests — but it was
missing from `py-modules`, so the installed console script could not import it
and `silicorism dashboard` failed with ModuleNotFoundError on every machine.
Nothing in the suite could see that, because the suite runs from the repo where
CWD is on sys.path.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Modules that are not part of the distribution.
_NOT_SHIPPED = {"conftest", "setup"}


def _py_modules() -> set[str]:
    """The py-modules list from pyproject, without needing tomllib (3.11+)."""
    text = (REPO / "pyproject.toml").read_text()
    block = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "pyproject has no py-modules list"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def test_every_top_level_module_is_shipped():
    on_disk = {p.stem for p in REPO.glob("*.py")} - _NOT_SHIPPED
    missing = on_disk - _py_modules()
    assert not missing, f"py-modules is missing: {sorted(missing)}"


def test_py_modules_names_nothing_that_does_not_exist():
    stale = _py_modules() - {p.stem for p in REPO.glob("*.py")}
    assert not stale, f"py-modules names files that are gone: {sorted(stale)}"


def test_every_console_script_target_is_callable():
    """A typo in an entry point is only visible after an install."""
    text = (REPO / "pyproject.toml").read_text()
    block = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", text, re.S)
    assert block, "pyproject has no console scripts"
    targets = re.findall(r'=\s*"([\w.]+):(\w+)"', block.group(1))
    assert targets, block.group(1)
    for module, func in targets:
        probe = (f"import importlib.util,sys;"
                 f"spec=importlib.util.spec_from_file_location('{module}',"
                 f"r'{REPO}/{module}.py');m=importlib.util.module_from_spec(spec);"
                 f"sys.modules['{module}']=m;spec.loader.exec_module(m);"
                 f"assert callable(m.{func})")
        r = subprocess.run([sys.executable, "-I", "-c", probe], cwd="/tmp",
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{module}:{func} -> {r.stderr}"
