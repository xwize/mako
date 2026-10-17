"""Cards a worker creates need the user's approval before anyone can claim them."""
from helpers import card, mcp_err, mcp_ok, mcp_session, ok


def set_approved(ws, cid: str, value: str) -> None:
    path = ws.root / "kanban" / "open" / f"{cid}.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("approved: false", f"approved: {value}")
    path.write_text(text, encoding="utf-8")


def create_issue_help(ws, agent: str) -> str:
    tools = mcp_session(ws, agent, [{"method": "tools/list"}])[1]["result"]["tools"]
    return next(t["description"] for t in tools if t["name"] == "create_issue")


def test_orchestrator_cards_are_approved(ws):
    assert "(approved" in ok(ws, "new", "feature", "Mine")
    out = mcp_ok(ws, "orchestrator", "create_issue", type="bug", title="Also mine")
    assert "(approved" in out and "not approved" not in out
    assert card(ws, "DEMO-0001")["approved"] == "true"
    assert card(ws, "DEMO-0002")["approved"] == "true"
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")


def test_create_issue_help_names_the_role(ws):
    help_text = create_issue_help(ws, "orchestrator")
    assert "You are the orchestrator" in help_text and "already approved" in help_text
    assert "a worker's issue" not in help_text
    assert "You are a worker slot" in create_issue_help(ws, "agent-a")


def test_worker_card_needs_approval_to_claim(ws):
    out = mcp_ok(ws, "agent-a", "create_issue", type="feature", title="Sneaky")
    assert "not approved yet" in out
    assert card(ws, "DEMO-0001")["approved"] == "false"
    assert "[unapproved]" in mcp_ok(ws, "agent-b", "list_issues")
    assert "not approved" in mcp_err(ws, "agent-b", "claim_issue", id="DEMO-0001")
    assert "no unclaimed" in mcp_err(ws, "agent-b", "claim_next")
    set_approved(ws, "DEMO-0001", "true")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0001")


def test_card_without_field_counts_as_approved(ws):
    ok(ws, "new", "feature", "Old")
    path = ws.root / "kanban" / "open" / "DEMO-0001.md"
    path.write_text(path.read_text(encoding="utf-8").replace("approved: true\n", ""), encoding="utf-8")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
