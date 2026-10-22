"""Mako's code is mako.py plus mako_*.py modules: init and scripts/upgrade.py install,
back up and restore them as one set, and running the installed copy leaves no __pycache__."""
from __future__ import annotations

import shutil
import sys

from helpers import REPO, SRC, ok, run

UPGRADE = REPO / "scripts" / "upgrade.py"


def code_files(folder) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.glob("mako*.py"))}


def test_init_installs_every_module(ws):
    installed = code_files(ws.root / ".mako")
    assert len(installed) > 1 and installed == code_files(SRC)


def test_the_installed_copy_runs_without_writing_pycache(ws):
    env = {k: v for k, v in ws.env.items() if k != "PYTHONDONTWRITEBYTECODE"}
    installed = ws.root / ".mako" / "mako.py"
    for args in (["new", "feature", "First"], ["list"], ["status"]):
        r = run([sys.executable, str(installed), *args], ws.root, env)
        assert r.code == 0, r.text
    r = run([sys.executable, str(installed), "mcp"], ws.root, {**env, "MAKO_AGENT": "agent-a"},
            stdin='{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n')
    assert r.code == 0 and '"claim_issue"' in r.out, r.text
    assert not [p for p in (ws.root / ".mako").rglob("__pycache__") if "worktrees" not in p.parts]


def test_init_removes_a_module_the_new_code_no_longer_has(ws):
    stale = ws.root / ".mako" / "mako_gone.py"
    stale.write_text("raise SystemExit('stale')\n", encoding="utf-8")
    assert "removed .mako/mako_gone.py" in ok(ws, "init")
    assert not stale.exists()


def single_file_install(ws) -> bytes:
    """Make .mako/ look like a single-file install: one mako.py, no modules."""
    for path in (ws.root / ".mako").glob("mako_*.py"):
        path.unlink()
    old = b"#!/usr/bin/env python3\n# the old single-file mako\nVERSION = \"0.0.9\"\n"
    (ws.root / ".mako" / "mako.py").write_bytes(old)
    return old


def test_upgrade_from_a_single_file_install_and_roll_back(ws, tmp_path):
    release = tmp_path / "release"  # outside any git repo: upgrade only warns
    shutil.copytree(SRC, release, ignore=shutil.ignore_patterns("__pycache__"))
    old = single_file_install(ws)
    up = [sys.executable, str(UPGRADE), "--workspace", str(ws.root)]

    r = run([*up, "--source", str(release / "mako.py")], ws.root, ws.env)
    assert r.code == 0, r.text
    assert code_files(ws.root / ".mako") == code_files(release)
    (backup,) = (ws.root / ".mako" / "backups").iterdir()
    assert code_files(backup) == {"mako.py": old}
    r = run([sys.executable, str(ws.root / ".mako" / "mako.py"), "list"], ws.root, ws.env)
    assert r.code == 0 and "Backlog" in r.out, r.text

    r = run([*up, "--rollback"], ws.root, ws.env)
    assert "Restored:" in r.out, r.text  # the dummy old mako.py can't run init afterwards; that's fine
    assert code_files(ws.root / ".mako") == {"mako.py": old}
