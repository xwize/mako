"""Dependencies: blocked claims, claim_next skipping, unblocking on close."""
from helpers import board, card, commit, fails, mcp_err, mcp_ok, ok


def test_blocked_claim_is_refused_until_dependency_is_done(ws):
    ok(ws, "new", "feature", "Base")
    ok(ws, "new", "feature", "Needs base", "--depends", "DEMO-0001")
    assert card(ws, "DEMO-0002")["depends_on"] == "[DEMO-0001]"
    assert "blocked by DEMO-0001" in mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0002")
    assert "#blocked" in (ws.root / "kanban" / "board.md").read_text(encoding="utf-8")

    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "base.txt", "base\n")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")
    assert "Claimed DEMO-0002" in mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_claim_next_skips_blocked_issues(ws):
    ok(ws, "new", "feature", "Base")
    ok(ws, "new", "feature", "Blocked", "--depends", "DEMO-0001")
    ok(ws, "new", "feature", "Free")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    # DEMO-0002 is next in the Backlog but blocked by DEMO-0001 (still in progress).
    assert "Claimed DEMO-0003" in mcp_ok(ws, "agent-b", "claim_next")
    assert board(ws)["backlog"] == ["DEMO-0002"]


def test_claim_next_with_nothing_claimable(ws):
    ok(ws, "new", "feature", "Base")
    ok(ws, "new", "feature", "Blocked", "--depends", "DEMO-0001")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    mcp_err(ws, "agent-b", "claim_next")


def test_dependency_cycles_and_unknown_ids_are_refused(ws):
    ok(ws, "new", "feature", "A")
    ok(ws, "new", "feature", "B", "--depends", "DEMO-0001")
    assert "cycle" in fails(ws, "depend", "DEMO-0001", "DEMO-0002")
    assert "can't depend on itself" in fails(ws, "depend", "DEMO-0001", "DEMO-0001")
    assert "DEMO-0099 not found" in fails(ws, "depend", "DEMO-0001", "DEMO-0099")
    ok(ws, "undepend", "DEMO-0002", "DEMO-0001")
    assert card(ws, "DEMO-0002")["depends_on"] == ""
