"""Broken slot worktrees (workspace last used on another OS) and mako repair."""
from helpers import commit, git, mcp_err, mcp_ok, ok


def break_links(ws, slot="agent-a"):
    """Rewrite both git links to Windows paths, as if the workspace was made on Windows."""
    # Git for Windows marks .git hidden, and Windows refuses mode "w" on a hidden file: r+ it.
    with open(ws.root / ".mako" / "worktrees" / slot / ".git", "r+", encoding="utf-8") as link:
        link.write(f"gitdir: S:/proj/{ws.repo.name}/.git/worktrees/{slot}\n")
        link.truncate()
    (ws.repo / ".git" / "worktrees" / slot / "gitdir").write_text(
        f"S:/proj/.mako/worktrees/{slot}/.git\n", encoding="utf-8")


def admin_dir(ws, slot="agent-a"):
    return ws.repo / ".git" / "worktrees" / slot


def test_init_keeps_the_admin_dir_of_a_broken_slot_and_points_at_repair(ws):
    break_links(ws)
    out = ok(ws, "init")
    assert "agent-a" in out and "mako repair" in out
    assert admin_dir(ws).is_dir()
    ok(ws, "repair")
    assert git(ws, "rev-parse", "--git-dir", cwd=ws.root / ".mako" / "worktrees" / "agent-a")


def test_claim_refuses_a_broken_slot_and_repairs_nothing(ws):
    ok(ws, "new", "feature", "One")
    break_links(ws)
    msg = mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "agent-a" in msg and "another OS" in msg and "mako repair" in msg
    assert "mako repair" in mcp_err(ws, "agent-a", "claim_next")
    link = (ws.root / ".mako" / "worktrees" / "agent-a" / ".git").read_text(encoding="utf-8")
    assert link.startswith("gitdir: S:/")


def test_repair_fixes_it_is_idempotent_and_the_loop_works_after(ws):
    ok(ws, "new", "feature", "One")
    break_links(ws)
    first = ok(ws, "repair")
    assert "agent-a: repaired" in first
    assert "agent-a: ok" in ok(ws, "repair")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "one.txt", "one\n")
    assert "closed DEMO-0001" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")


def test_repair_on_a_healthy_workspace_changes_nothing(ws):
    assert "agent-a: ok" in ok(ws, "repair")


def test_repair_is_in_the_help_and_the_cli_prompt(ws):
    assert "repair" in ok(ws, "--help")
    assert "mako repair" in (ws.root / ".mako" / "prompts" / "cli.txt").read_text(encoding="utf-8")
