"""The core loop (new -> claim -> commit -> close), close guards and claim locks."""
from helpers import NOW, branch_exists, card, commit, fails, git, mcp_err, mcp_ok, ok


def test_core_loop(ws):
    assert "Created DEMO-0001: First feature" in ok(ws, "new", "feature", "First feature")
    text = mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "branch:   feature/DEMO-0001-first-feature" in text
    assert card(ws, "DEMO-0001")["status"] == "in-progress"
    assert card(ws, "DEMO-0001")["claimed_by"] == "agent-a"
    commit(ws, "agent-a", "hello.txt", "hello\n", "DEMO-0001: hello")

    text = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="Added hello.")
    assert "Merged feature/DEMO-0001-first-feature" in text

    c = card(ws, "DEMO-0001")
    assert (c["_folder"], c["status"], c["merged"], c["closed"]) == ("closed", "done", NOW, NOW)
    assert "Added hello." in c["_body"]
    assert git(ws, "log", "-1", "--format=%s", "main") == "Merge DEMO-0001: First feature"
    assert (ws.repo / "hello.txt").is_file()
    assert not branch_exists(ws, "feature/DEMO-0001-first-feature")
    # The slot is idle, detached at main.
    assert "current claim: none" in mcp_ok(ws, "agent-a", "whoami")
    wt = ws.worktree("agent-a")
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "HEAD"
    assert git(ws, "rev-parse", "HEAD", cwd=wt) == git(ws, "rev-parse", "main")


def test_close_without_commits_merges_nothing(ws):
    ok(ws, "new", "bug", "Nothing to do")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    before = git(ws, "rev-parse", "main")
    assert "nothing to merge" in ok(ws, "close", "DEMO-0001")
    assert git(ws, "rev-parse", "main") == before
    assert card(ws, "DEMO-0001")["merged"] == ""


def test_close_refuses_branch_behind_main(ws):
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0002")
    commit(ws, "agent-a", "a.txt", "a\n")
    commit(ws, "agent-b", "b.txt", "b\n")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="first")

    assert "is behind main" in mcp_err(ws, "agent-b", "close_issue", id="DEMO-0002", note="second")
    assert card(ws, "DEMO-0002")["status"] == "in-progress"
    # After merging main in its own worktree, it closes.
    git(ws, "merge", "-q", "main", cwd=ws.worktree("agent-b"))
    mcp_ok(ws, "agent-b", "close_issue", id="DEMO-0002", note="second")
    assert (ws.repo / "a.txt").is_file() and (ws.repo / "b.txt").is_file()


def test_close_refuses_dirty_main_checkout(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    (ws.repo / "stray.txt").write_text("x", encoding="utf-8")
    git(ws, "add", "stray.txt")
    assert "has uncommitted changes" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert card(ws, "DEMO-0001")["status"] == "in-progress"


def test_close_refuses_uncommitted_changes_in_slot(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    (ws.worktree("agent-a") / "a.txt").write_text("changed\n", encoding="utf-8")
    assert "uncommitted changes" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert card(ws, "DEMO-0001")["status"] == "in-progress"


def test_second_claim_of_same_issue_fails(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "already claimed by agent-a" in mcp_err(ws, "agent-b", "claim_issue", id="DEMO-0001")
    # Claiming your own claim again is harmless.
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")


def test_one_claim_per_slot(ws):
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "already holds DEMO-0001" in mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_worker_cannot_close_or_release_another_slots_claim(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "claimed by agent-a, not agent-b" in mcp_err(ws, "agent-b", "close_issue", id="DEMO-0001", note="x")
    assert "claimed by agent-a, not agent-b" in mcp_err(ws, "agent-b", "release_issue", id="DEMO-0001")
    assert card(ws, "DEMO-0001")["claimed_by"] == "agent-a"


def test_orchestrator_cannot_claim(ws):
    ok(ws, "new", "feature", "One")
    assert "only worker slots claim" in mcp_err(ws, "orchestrator", "claim_issue", id="DEMO-0001")


def test_release_keeps_branch_and_next_claim_resumes_it(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    sha = commit(ws, "agent-a", "a.txt", "a\n")
    mcp_ok(ws, "agent-a", "release_issue", id="DEMO-0001", note="stuck")
    c = card(ws, "DEMO-0001")
    assert (c["status"], c["claimed_by"]) == ("backlog", "")
    assert "stuck" in c["_body"]
    assert branch_exists(ws, "feature/DEMO-0001-one")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0001")
    assert git(ws, "rev-parse", "HEAD", cwd=ws.worktree("agent-b")) == sha


def test_cli_errors_exit_nonzero(ws):
    assert "not found" in fails(ws, "show", "DEMO-0099")
    assert "invalid choice: 'chore'" in fails(ws, "new", "chore", "x")
    assert "type must be one of" in mcp_err(ws, "orchestrator", "create_issue", type="chore", title="x")
