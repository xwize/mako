"""merge_to: an optional branch that closing merges into instead of the base branch."""
from helpers import branch_exists, card, commit, fails, git, mcp_err, mcp_ok, ok, set_config

ONE = "feature/DEMO-0001-one"


def finish(ws, slot="agent-a"):
    mcp_ok(ws, slot, "claim_issue", id="DEMO-0001")
    commit(ws, slot, "one.txt", "one\n")
    return mcp_ok(ws, slot, "close_issue", id="DEMO-0001", note="done")


def test_merge_to_creates_the_branch_and_leaves_main_on_base(ws):
    ok(ws, "new", "feature", "One", "--merge-to", "feature/auth")
    assert card(ws, "DEMO-0001")["merge_to"] == "feature/auth"
    assert "merge_to: feature/auth" in ok(ws, "show", "DEMO-0001")
    base_head = git(ws, "rev-parse", "main")
    assert "into feature/auth" in finish(ws)
    assert git(ws, "rev-parse", "main") == base_head
    assert git(ws, "branch", "--show-current", cwd=ws.repo) == "main"
    assert git(ws, "log", "-1", "--format=%s", "feature/auth") == "Merge DEMO-0001: One"
    assert git(ws, "show", "feature/auth:one.txt") == "one"
    assert not branch_exists(ws, ONE)
    assert card(ws, "DEMO-0001")["status"] == "done"


def test_merge_to_existing_branch_and_close_message(ws):
    git(ws, "branch", "feature/auth", "main")
    ok(ws, "new", "feature", "One", "--merge-to", "feature/auth")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "one.txt", "one\n")
    out = ok(ws, "close", "DEMO-0001", "--agent", "agent-a")
    assert out.startswith(f"Merged {ONE} into feature/auth and closed DEMO-0001")
    assert git(ws, "branch", "--show-current", cwd=ws.repo) == "main"


def test_without_merge_to_merges_into_base_as_before(ws):
    ok(ws, "new", "feature", "One")
    assert "merge_to" not in card(ws, "DEMO-0001")
    finish(ws)
    assert git(ws, "log", "-1", "--format=%s", "main") == "Merge DEMO-0001: One"


def test_edit_sets_and_clears_merge_to(ws):
    ok(ws, "new", "feature", "One")
    ok(ws, "edit", "DEMO-0001", "--merge-to", "feature/auth")
    assert card(ws, "DEMO-0001")["merge_to"] == "feature/auth"
    mcp_ok(ws, "agent-a", "edit_issue", id="DEMO-0001", merge_to="")
    assert "merge_to" not in card(ws, "DEMO-0001")
    mcp_ok(ws, "agent-a", "create_issue", type="bug", title="Two", merge_to="fix/x")
    assert card(ws, "DEMO-0002")["merge_to"] == "fix/x"


def test_merge_to_is_refused_for_spikes_and_bad_names_and_claimed_cards(ws):
    assert "not for spikes" in fails(ws, "new", "spike", "Try", "--merge-to", "x")
    ok(ws, "new", "feature", "One")
    assert "not a valid branch name" in fails(ws, "edit", "DEMO-0001", "--merge-to", "bad..name")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "in progress" in mcp_err(ws, "agent-a", "edit_issue", id="DEMO-0001", merge_to="x")


def test_failed_merge_into_merge_to_leaves_everything_unchanged(ws):
    ok(ws, "new", "feature", "One", "--merge-to", "feature/auth")
    git(ws, "switch", "-c", "feature/auth", cwd=ws.repo)
    commit_in_repo = ws.repo / "one.txt"
    commit_in_repo.write_text("conflict\n", encoding="utf-8")
    git(ws, "add", "one.txt", cwd=ws.repo)
    git(ws, "commit", "-q", "-m", "conflicting", cwd=ws.repo)
    git(ws, "switch", "main", cwd=ws.repo)
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "one.txt", "one\n")
    assert "into feature/auth failed" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert git(ws, "branch", "--show-current", cwd=ws.repo) == "main"
    assert git(ws, "status", "--porcelain", cwd=ws.repo) == ""
    assert card(ws, "DEMO-0001")["status"] == "in-progress"


def test_prune_judges_a_merge_to_branch_against_its_target(ws):
    set_config(ws, keep_branches=True)
    ok(ws, "new", "feature", "One", "--merge-to", "feature/auth")
    finish(ws)
    assert branch_exists(ws, ONE)
    assert ok(ws, "prune").strip() == f"deleted {ONE} (DEMO-0001)"
    assert not branch_exists(ws, ONE)
