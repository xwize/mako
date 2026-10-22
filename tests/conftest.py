"""pytest fixtures for Mako's regression tests. Helpers are in helpers.py."""
from __future__ import annotations

import pytest
from helpers import Workspace, isolated_env, new_workspace, set_config


@pytest.fixture(scope="session")
def base_env(tmp_path_factory) -> dict:
    return isolated_env(tmp_path_factory.mktemp("home"))


@pytest.fixture
def ws(tmp_path, base_env) -> Workspace:
    """A fresh workspace: `mako init demo` with slots agent-a and agent-b. auto_approve is on,
    so close_issue isn't gated on the default manual review; review tests turn it off."""
    w = new_workspace(tmp_path / "ws", base_env)
    set_config(w, auto_approve=True)
    return w
