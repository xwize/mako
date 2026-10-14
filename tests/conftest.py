"""pytest fixtures for Mako's regression tests. Helpers are in helpers.py."""
from __future__ import annotations

import pytest
from helpers import Workspace, isolated_env, new_workspace


def pytest_addoption(parser):
    parser.addoption("--update-snapshots", action="store_true",
                     help="rewrite tests/snapshots/ from the current output instead of comparing")


@pytest.fixture(scope="session")
def base_env(tmp_path_factory) -> dict:
    return isolated_env(tmp_path_factory.mktemp("home"))


@pytest.fixture
def ws(tmp_path, base_env) -> Workspace:
    """A fresh workspace: `mako init demo` with slots agent-a and agent-b."""
    return new_workspace(tmp_path / "ws", base_env)
