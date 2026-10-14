"""Spikes close without merging; their branch is kept."""
from helpers import branch_exists, card, commit, drag, git, mcp_ok, ok, set_config


def test_spike_closes_without_merging_even_when_behind(ws):
    ok(ws, "new", "spike", "Try it")
    ok(ws, "new", "feature", "Other")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "spike.txt", "findings\n")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0002")
    commit(ws, "agent-b", "b.txt", "b\n")
    mcp_ok(ws, "agent-b", "close_issue", id="DEMO-0002", note="first")  # main moves on
    main_before = git(ws, "rev-parse", "main")

    text = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="It works.")
    assert text == "Closed spike DEMO-0001 without merging; branch kept: spike/DEMO-0001-try-it."
    assert git(ws, "rev-parse", "main") == main_before
    assert branch_exists(ws, "spike/DEMO-0001-try-it")
    c = card(ws, "DEMO-0001")
    assert (c["status"], c["merged"]) == ("done", "")
    assert "Spike closed without merging; branch kept: spike/DEMO-0001-try-it." in c["_body"]
    assert "It works." in c["_body"]
    assert "[not merged]" in ok(ws, "list", "--all")
    assert "[[DEMO-0001]] Try it #spike #not-merged" in (ws.root / "kanban" / "board.md").read_text(encoding="utf-8")
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD", cwd=ws.worktree("agent-a")) == "HEAD"


def test_spike_is_not_review_gated(ws):
    set_config(ws, review_command="python -c pass")
    ok(ws, "new", "spike", "Try it")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "spike.txt", "findings\n")
    assert "without merging" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_spike_dragged_to_done(ws):
    ok(ws, "new", "spike", "Try it")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "spike.txt", "findings\n")
    drag(ws, "DEMO-0001", "done")
    ok(ws, "list")
    assert card(ws, "DEMO-0001")["status"] == "done"
    assert branch_exists(ws, "spike/DEMO-0001-try-it")
    assert not (ws.repo / "spike.txt").exists()


def test_spike_without_commits_leaves_no_branch(ws):
    ok(ws, "new", "spike", "Empty")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    assert ok(ws, "close", "DEMO-0001").strip() == "Closed spike DEMO-0001 without merging."
    assert not branch_exists(ws, "spike/DEMO-0001-empty")


def test_reopened_spike_retyped_as_feature_merges(ws):
    ok(ws, "new", "spike", "Grows up")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "s.txt", "s\n")
    ok(ws, "close", "DEMO-0001")
    ok(ws, "reopen", "DEMO-0001")
    ok(ws, "edit", "DEMO-0001", "--type", "feature")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    assert "Merged spike/DEMO-0001-grows-up" in ok(ws, "close", "DEMO-0001")
    assert (ws.repo / "s.txt").is_file()
