"""skip_base_merge_on_close: close does everything but the merge into the base branch."""
import json

from helpers import (branch_exists, card, commit, git, mcp_err, mcp_instructions, mcp_ok, ok,
                     set_config)

ONE = "feature/DEMO-0001-one"


def claim_and_commit(ws, slot="agent-a"):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, slot, "claim_issue", id="DEMO-0001")
    commit(ws, slot, "one.txt", "one\n")


def test_init_lists_the_setting_off(ws):
    path = ws.root / ".mako" / "config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["skip_base_merge_on_close"] is False


def test_off_merges_as_before(ws):
    claim_and_commit(ws)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")
    assert out == f"Merged {ONE} into the base branch and closed DEMO-0001."
    assert git(ws, "log", "-1", "--format=%s", "main") == "Merge DEMO-0001: One"
    assert "skip_base_merge_on_close" not in mcp_instructions(ws, "agent-a")


def test_on_skips_the_merge(ws):
    set_config(ws, skip_base_merge_on_close=True)
    main = git(ws, "rev-parse", "main")
    claim_and_commit(ws)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")
    assert out == "Closed DEMO-0001 without merging (skip_base_merge_on_close)."
    assert git(ws, "rev-parse", "main") == main
    assert not branch_exists(ws, ONE)  # keep_branches is off
    c = card(ws, "DEMO-0001")
    assert c["status"] == "done" and not c["merged"]
    assert "Closed without merging" in c["_body"]
    # The slot is free again and the next issue can be claimed.
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")
    assert "skip_base_merge_on_close" in mcp_instructions(ws, "agent-a")


def test_on_with_keep_branches_keeps_the_branch(ws):
    set_config(ws, skip_base_merge_on_close=True, keep_branches=True)
    claim_and_commit(ws)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")
    assert out == f"Closed DEMO-0001 without merging (skip_base_merge_on_close); branch kept: {ONE}."
    assert branch_exists(ws, ONE)
    assert "DEMO-0001" not in git(ws, "log", "--format=%s", "main")


def test_on_still_refuses_a_branch_behind_the_base(ws):
    set_config(ws, skip_base_merge_on_close=True)
    claim_and_commit(ws)
    (ws.repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(ws, "add", "base.txt", cwd=ws.repo)
    git(ws, "commit", "-m", "base moves", cwd=ws.repo)
    assert "is behind main" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")
