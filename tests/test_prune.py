"""keep_branches (close keeps the merged branch and names it) and mako prune."""
import json

from helpers import branch_exists, card, commit, git, mcp_ok, ok, set_config

ONE = "feature/DEMO-0001-one"


def close_one(ws, slot="agent-a"):
    """new, claim, commit and close DEMO-0001 ("One")."""
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, slot, "claim_issue", id="DEMO-0001")
    commit(ws, slot, "one.txt", "one\n")
    return mcp_ok(ws, slot, "close_issue", id="DEMO-0001", note="done")


def test_init_lists_keep_branches_and_never_changes_it(ws):
    path = ws.root / ".mako" / "config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["keep_branches"] is False
    set_config(ws, keep_branches=True)
    ok(ws, "init")
    assert json.loads(path.read_text(encoding="utf-8"))["keep_branches"] is True
    config = json.loads(path.read_text(encoding="utf-8"))
    del config["keep_branches"]
    path.write_text(json.dumps(config), encoding="utf-8")
    ok(ws, "init")
    assert json.loads(path.read_text(encoding="utf-8"))["keep_branches"] is False


def test_default_deletes_the_branch_and_prune_finds_nothing(ws):
    close_one(ws)
    assert git(ws, "log", "-1", "--format=%s", "main") == "Merge DEMO-0001: One"
    assert not branch_exists(ws, ONE)
    assert ok(ws, "prune").strip() == "Nothing to prune."


def test_keep_branches_keeps_and_names_the_branch_then_prune_deletes_it(ws):
    set_config(ws, keep_branches=True)
    assert close_one(ws) == f"Merged {ONE} into the base branch and closed DEMO-0001; branch kept."
    assert git(ws, "log", "-1", "--format=%s", "main") == f"Merge {ONE} (DEMO-0001): One"
    assert git(ws, "log", "--grep", "DEMO-0001", "--format=%s", "main").startswith("Merge ")
    assert branch_exists(ws, ONE)

    assert ok(ws, "prune", "--dry-run").strip() == f"would delete {ONE} (DEMO-0001)"
    assert branch_exists(ws, ONE)
    assert ok(ws, "prune").strip() == f"deleted {ONE} (DEMO-0001)"
    assert not branch_exists(ws, ONE)


def test_keep_branches_still_deletes_a_branch_that_never_had_commits(ws):
    set_config(ws, keep_branches=True)
    ok(ws, "new", "bug", "Nothing")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    assert "nothing to merge" in ok(ws, "close", "DEMO-0001")
    assert not branch_exists(ws, "bug/DEMO-0001-nothing")


def test_prune_never_touches_backlog_or_in_progress_branches(ws):
    set_config(ws, keep_branches=True)
    close_one(ws)
    ok(ws, "new", "feature", "Busy")
    ok(ws, "new", "feature", "Released")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")
    commit(ws, "agent-a", "busy.txt", "busy\n")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0003")
    mcp_ok(ws, "agent-b", "release_issue", id="DEMO-0003")

    out = ok(ws, "prune")
    assert out.strip() == f"deleted {ONE} (DEMO-0001)"
    assert branch_exists(ws, "feature/DEMO-0002-busy")
    assert branch_exists(ws, "feature/DEMO-0003-released")
    out = ok(ws, "prune", "DEMO-0002", "DEMO-0003")
    assert "skipped DEMO-0002: In Progress issues keep their branch" in out
    assert "skipped DEMO-0003: Backlog issues keep their branch" in out
    assert branch_exists(ws, "feature/DEMO-0002-busy")


def test_prune_by_id_and_skips_a_spike_branch(ws):
    set_config(ws, keep_branches=True)
    close_one(ws)
    ok(ws, "new", "feature", "Two")
    ok(ws, "claim", "DEMO-0002", "--agent", "agent-b")
    commit(ws, "agent-b", "two.txt", "two\n")
    ok(ws, "close", "DEMO-0002")
    ok(ws, "new", "spike", "Look")
    ok(ws, "claim", "DEMO-0003", "--agent", "agent-a")
    commit(ws, "agent-a", "look.txt", "look\n")
    ok(ws, "close", "DEMO-0003")

    assert ok(ws, "prune", "2").strip() == "deleted feature/DEMO-0002-two (DEMO-0002)"
    assert branch_exists(ws, ONE)
    out = ok(ws, "prune")
    assert f"deleted {ONE} (DEMO-0001)" in out
    assert ("skipped spike/DEMO-0003-look (DEMO-0003): not fully merged, "
            "1 commit not in main (a spike's branch)") in out
    assert branch_exists(ws, "spike/DEMO-0003-look")


def test_prune_skips_a_branch_checked_out_in_a_worktree(ws):
    set_config(ws, keep_branches=True)
    close_one(ws)
    git(ws, "switch", ONE, cwd=ws.worktree("agent-b"))
    assert ok(ws, "prune").strip() == f"skipped {ONE} (DEMO-0001): checked out in a worktree"
    assert branch_exists(ws, ONE)


def test_prune_discarded_only_with_the_flag(ws):
    ok(ws, "new", "feature", "Doomed")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    commit(ws, "agent-a", "b.txt", "b\n")
    assert "`mako prune --discarded DEMO-0001`" in ok(ws, "discard", "DEMO-0001")
    doomed = "feature/DEMO-0001-doomed"

    assert ok(ws, "prune").strip() == "Nothing to prune."
    assert "add --discarded to delete it" in ok(ws, "prune", "DEMO-0001")
    out = ok(ws, "prune", "--discarded", "--dry-run")
    assert out.strip() == f"would delete {doomed} (DEMO-0001), losing 2 unmerged commits"
    assert branch_exists(ws, doomed)
    assert ok(ws, "prune", "--discarded").strip() == f"deleted {doomed} (DEMO-0001), losing 2 unmerged commits"
    assert not branch_exists(ws, doomed)


def test_reopen_and_claim_with_the_branch_kept_resumes_it(ws):
    set_config(ws, keep_branches=True)
    close_one(ws)
    old_tip = git(ws, "rev-parse", ONE)
    ok(ws, "reopen", "DEMO-0001")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    wt = ws.worktree("agent-a")
    assert git(ws, "rev-parse", "HEAD", cwd=wt) == old_tip
    git(ws, "merge", "-q", "main", cwd=wt)
    commit(ws, "agent-a", "more.txt", "more\n")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="again")
    assert (ws.repo / "more.txt").is_file()
    assert branch_exists(ws, ONE)


def test_reopen_and_closing_again_with_no_new_commits_keeps_the_branch(ws):
    set_config(ws, keep_branches=True)
    close_one(ws)
    ok(ws, "reopen", "DEMO-0001")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    ok(ws, "close", "DEMO-0001")
    assert branch_exists(ws, ONE)


def test_reopen_and_claim_after_prune_starts_fresh_from_main(ws):
    set_config(ws, keep_branches=True)
    close_one(ws)
    ok(ws, "prune")
    ok(ws, "reopen", "DEMO-0001")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    wt = ws.worktree("agent-a")
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == ONE
    assert git(ws, "rev-parse", "HEAD", cwd=wt) == git(ws, "rev-parse", "main")
    assert card(ws, "DEMO-0001")["branch"] == ONE
