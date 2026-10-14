"""Run Mako's checks: ruff (if available), then the regression tests in tests/.

    python scripts/test.py             everything
    python scripts/test.py -k claim    extra arguments go to pytest

Development only: pytest and ruff are not needed to install or run Mako. pytest
is used from this Python if installed (`pip install pytest`), otherwise through
`uvx pytest` if uv is installed. Exits non-zero if anything fails.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(cmd: list[str], env: dict) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=REPO, env=env, check=False).returncode


def ruff(env: dict) -> int:
    args = ["check", "--no-cache", "--config", "dev/ruff.toml", "src", "scripts", "tests"]
    if shutil.which("ruff"):
        return run(["ruff", *args], env)
    if importlib.util.find_spec("ruff"):
        return run([sys.executable, "-m", "ruff", *args], env)
    if shutil.which("uvx"):
        # --offline: use a cached ruff if there is one, never download during a test run.
        probe = subprocess.run(["uvx", "--offline", "ruff", "--version"], cwd=REPO, env=env,
                               capture_output=True, check=False)
        if probe.returncode == 0:
            return run(["uvx", "--offline", "ruff", *args], env)
    print("note: ruff not found, lint skipped (pip install ruff, or run `uvx ruff --version` once)")
    return 0


def pytest_cmd() -> list[str] | None:
    if importlib.util.find_spec("pytest"):
        return [sys.executable, "-m", "pytest"]
    if shutil.which("uvx"):
        return ["uvx", "pytest"]
    return None


def main() -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for key in [k for k in env if k.startswith("MAKO_")]:
        del env[key]  # a Mako pane's MAKO_AGENT must not leak into the tests
    cmd = pytest_cmd()
    if cmd is None:
        print("pytest is not installed. Install it with `pip install pytest`, or install uv "
              "(https://docs.astral.sh/uv/) so `uvx pytest` works.", file=sys.stderr)
        return 2
    lint = ruff(env)
    # No cache dir: a run leaves nothing behind outside pytest's temp folder.
    tests = run([*cmd, "tests", "-p", "no:cacheprovider", *sys.argv[1:]], env)
    if lint:
        print("ruff found problems (see above)", file=sys.stderr)
    return 1 if (lint or tests) else 0


if __name__ == "__main__":
    sys.exit(main())
