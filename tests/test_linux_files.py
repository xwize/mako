"""The POSIX files init writes next to the Windows ones: mako.sh, mako.launch.sh, agent.sh."""
import json
import os
import shutil
import stat
import sys

import pytest
from helpers import SRC, fails, mako, new_workspace, ok, run

pytestmark = pytest.mark.skipif(os.name == "nt", reason="runs bash scripts")

FILES = ("mako.sh", "mako.launch.sh", ".mako/agent.sh", ".mako/bin/mako")


def stub_claude(tmp_path):
    """A `claude` on PATH that records its arguments, one per line."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    log = tmp_path / "claude-args.txt"
    exe = bin_dir / "claude"
    exe.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "{log}"; done\n', encoding="utf-8")
    exe.chmod(0o755)
    return bin_dir, log


def agent_sh(ws, tmp_path, *args):
    bin_dir, log = stub_claude(tmp_path)
    env = {**ws.env, "PATH": f"{bin_dir}{os.pathsep}{ws.env['PATH']}", "SHELL": "/bin/true"}
    r = run(["bash", str(ws.root / ".mako" / "agent.sh"), *args], ws.root, env)
    assert r.code == 0, r.text
    return r, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def test_init_writes_the_posix_files_executable_and_lf_only(ws):
    for name in FILES:
        path = ws.root / name
        assert path.is_file(), name
        assert path.stat().st_mode & stat.S_IXUSR, name
        assert b"\r" not in path.read_bytes(), name


def test_init_converts_crlf_templates_to_lf(tmp_path, base_env):
    rel = tmp_path / "release"
    shutil.copytree(SRC, rel)
    for name in ("mako.launch.sh", "agent.sh"):
        path = rel / "templates" / "posix" / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    w = new_workspace(tmp_path / "ws2", base_env, script=rel / "mako.py")
    for name in ("mako.launch.sh", ".mako/agent.sh"):
        assert b"\r" not in (w.root / name).read_bytes(), name


def test_rerunning_init_leaves_the_posix_files_alone(ws):
    out = ok(ws, "init")
    assert "created " not in out and "updated " not in out


def test_gitattributes_keeps_shell_scripts_lf():
    assert "*.sh text eol=lf" in (SRC.parent / ".gitattributes").read_text(encoding="utf-8")


def test_mako_sh_and_the_bin_shim_work_with_only_python3(ws, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, target in (("python3", sys.executable), ("dirname", shutil.which("dirname")),
                         ("readlink", shutil.which("readlink")), ("git", shutil.which("git"))):
        (tools / name).symlink_to(target)
    env = {**ws.env, "PATH": str(tools)}
    for script in (ws.root / "mako.sh", ws.root / ".mako" / "bin" / "mako"):
        r = run(["/bin/sh", str(script), "list"], ws.root, env)
        assert r.code == 0 and "Backlog" in r.out, f"{script}: {r.text}"


def test_agent_sh_passes_the_agent_cmd_arguments(ws, tmp_path):
    tag = json.loads((ws.root / ".mako" / "config.json").read_text(encoding="utf-8"))["session_tag"]
    _, args = agent_sh(ws, tmp_path, "agent-a")
    mcp = json.loads(args[args.index("--mcp-config") + 1])
    assert mcp["mcpServers"]["mako"]["env"] == {"MAKO_AGENT": "agent-a"}
    assert mcp["mcpServers"]["mako"]["args"] == [str(ws.root / ".mako" / "mako.py"), "mcp"]
    assert "-c" not in args
    assert args[:2] == ["-n", f"demo-agent-a-{tag}"]
    assert args[2:4] == ["--add-dir", str(ws.root)]
    assert args[-2:] == ["--allowedTools", "mcp__mako"]


def test_agent_sh_resume_without_a_conversation_starts_fresh(ws, tmp_path):
    r, args = agent_sh(ws, tmp_path, "agent-a", "resume")
    assert "No earlier conversation for agent-a" in r.out and "-c" not in args


def test_agent_sh_orchestrator_adds_the_project_repo(ws, tmp_path):
    _, args = agent_sh(ws, tmp_path, "orchestrator")
    assert args[args.index("--add-dir") + 1] == str(ws.root / "demo")


def test_agent_sh_warns_about_a_broken_slot_and_repairs_nothing(ws, tmp_path):
    link = ws.root / ".mako" / "worktrees" / "agent-a" / ".git"
    link.write_text("gitdir: S:/proj/demo/.git/worktrees/agent-a\n", encoding="utf-8")
    r, _ = agent_sh(ws, tmp_path, "agent-a")
    assert "agent-a's worktree is broken" in r.out and "mako repair" in r.out
    assert link.read_text(encoding="utf-8").startswith("gitdir: S:/")
    (tmp_path / "again").mkdir()
    assert "broken" not in agent_sh(ws, tmp_path / "again", "orchestrator")[0].out


def test_launching_a_single_slot_is_not_supported_yet(ws):
    assert "single slot is not supported on Linux" in fails(ws, "launch", "agent-a")
