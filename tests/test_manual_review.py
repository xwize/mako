"""The manual review step: with no review_hook and no backend, request_review tells the agent to
show the user the change and close_issue is gated on it, unless auto_approve is on."""
import json

from helpers import card, commit, git, mcp_err, mcp_guide, mcp_ok, mcp_tools, ok, set_config
from test_review import flag_command
from test_review_backend import use_stub


def manual(ws) -> None:
    """The ws fixture turns auto_approve on; drop it, as a workspace without the setting has."""
    path = ws.root / ".mako" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.pop("auto_approve")
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def test_init_writes_auto_approve_off_and_keeps_it(ws):
    path = ws.root / ".mako" / "config.json"
    manual(ws)
    ok(ws, "init")
    assert json.loads(path.read_text(encoding="utf-8"))["auto_approve"] is False
    set_config(ws, auto_approve=True)
    ok(ws, "init")
    assert json.loads(path.read_text(encoding="utf-8"))["auto_approve"] is True


def test_default_gates_close_until_request_review_covers_head(ws):
    manual(ws)
    assert "request_review" in mcp_tools(ws, "agent-a")
    assert "review_status" not in mcp_tools(ws, "agent-a")
    ok(ws, "new", "feature", "Manual")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    sha = commit(ws, "agent-a", "a.txt", "a\n")
    assert f"call `request_review` first (no review for {sha[:7]})" in \
        mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")

    text = mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert text.startswith(f"Recorded the review of DEMO-0001 ({sha[:7]}). No review tool is configured")
    assert "git diff main...feature/DEMO-0001-manual --stat" in text
    assert "a.txt | 1 +" in text
    assert "End your turn and wait" in text and '"approve"' in text
    assert card(ws, "DEMO-0001")["reviews"] == "1"

    sha2 = commit(ws, "agent-a", "b.txt", "b\n")
    assert f"no review for {sha2[:7]}" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert "Merged" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_default_guide_describes_the_manual_review(ws):
    manual(ws)
    worker = mcp_guide(ws, "agent-a")
    assert "## Code review" in worker and "tells you what to show them" in worker
    assert "`mako review <ID>` (show the change and record it as reviewed)" in worker
    set_config(ws, auto_approve=True)
    assert "## Code review" not in mcp_guide(ws, "agent-a")


def test_mako_review_records_the_manual_review_for_the_human(ws):
    manual(ws)
    ok(ws, "new", "feature", "One")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    sha = commit(ws, "agent-a", "a.txt", "a\n")
    out = ok(ws, "review", "DEMO-0001")
    assert out.startswith(f"Recorded the review of DEMO-0001 ({sha[:7]}) on feature/DEMO-0001-one:")
    assert "a.txt" in out
    assert "Merged" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_auto_approve_has_no_review_step(ws):
    set_config(ws, auto_approve=True)
    assert "request_review" not in mcp_tools(ws, "agent-a")
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "auto_approve is on" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    assert "Merged" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_auto_approve_is_ignored_with_a_review_hook(ws, tmp_path):
    set_config(ws, auto_approve=True, review_hook=flag_command(tmp_path / "review.flag"))
    assert "request_review" in mcp_tools(ws, "agent-a")
    ok(ws, "new", "feature", "Hooked")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "call `request_review` first" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    text = mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert "Review opened for DEMO-0001" in text
    assert "note: auto_approve is ignored while a review step is set" in text
    assert "Merged" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_auto_approve_is_ignored_with_a_review_backend(ws):
    use_stub(ws)
    set_config(ws, auto_approve=True)
    assert "request_review" in mcp_tools(ws, "agent-a")
    ok(ws, "new", "feature", "Backed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "call `request_review` first" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    text = mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert text.startswith("stub review of DEMO-0001") and "auto_approve is ignored" in text


def test_spike_and_cli_close_are_not_gated_by_default(ws):
    manual(ws)
    ok(ws, "new", "spike", "Try it")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "spike.txt", "findings\n")
    assert "without merging" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")

    ok(ws, "new", "feature", "Human closes")
    ok(ws, "claim", "DEMO-0002", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "Merged" in ok(ws, "close", "DEMO-0002", "--force")
    assert git(ws, "log", "-1", "--format=%s", "main") == "Merge DEMO-0002: Human closes"
