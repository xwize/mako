"""Editing a card's description and title: the user's own `## ` headings, Mako's headings."""
from helpers import board, card, card_file, fails, mcp_err, mcp_ok, ok

DESC = "## Goal\n\nShip it.\n\n## Plan\n\n1. Do the thing."


def test_new_keeps_user_headings(ws):
    ok(ws, "new", "feature", "Thing", "--body", DESC)
    assert DESC in card(ws, "DEMO-0001")["_body"]


def test_edit_replaces_whole_description_with_headings(ws):
    ok(ws, "new", "feature", "Thing", "--body", DESC)
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    ok(ws, "release", "DEMO-0001", "--agent", "agent-a", "--note", "later")
    before = card(ws, "DEMO-0001")["_body"]
    tail = before[before.index("## Acceptance criteria"):]
    assert "## Released (agent-a" in tail

    ok(ws, "edit", "DEMO-0001", "--description", "## Goal\n\nSecond.")
    mcp_ok(ws, "orchestrator", "edit_issue", id="DEMO-0001", description="## Goal\n\nThird.\n\n## Risks\n\nNone.")
    body = card(ws, "DEMO-0001")["_body"]
    assert "Ship it." not in body and "Second." not in body and "Do the thing" not in body
    assert body.count("## Goal") == 1
    assert "## Description\n\n## Goal\n\nThird.\n\n## Risks\n\nNone.\n\n## Acceptance criteria" in body
    assert body.endswith(tail)
    assert "DEMO-0001" in " ".join(sum(board(ws).values(), []))


def test_mako_headings_in_description_are_refused(ws):
    for heading in ("## Acceptance criteria", "## Notes", "## Description"):
        text = f"Intro.\n\n{heading}\n\n- [ ] x"
        assert f"'{heading}' is a card section" in fails(ws, "new", "bug", "B", "--body", text)
        assert f"'{heading}' is a card section" in mcp_err(ws, "orchestrator", "create_issue",
                                                          type="bug", title="B", description=text)
    assert not (ws.root / "kanban" / "open" / "DEMO-0001.md").exists()

    ok(ws, "new", "bug", "B", "--body", "Old.")
    out = fails(ws, "edit", "DEMO-0001", "--description", "## Acceptance criteria\n\n- [ ] x")
    assert "use ### inside the description, or fill in the card's own section" in out
    assert "## Released (agent-a, now)" in mcp_err(ws, "orchestrator", "edit_issue", id="DEMO-0001",
                                                    description="## Released (agent-a, now)\n\nhi")
    assert "Old." in card(ws, "DEMO-0001")["_body"]


def test_mako_headings_under_h3_or_in_fences_are_accepted(ws):
    text = "### Acceptance criteria\n\n- [ ] x\n\n```md\n## Notes\n## Description\n```\n\n~~~\n## Notes\n~~~"
    ok(ws, "new", "feature", "F", "--body", text)
    ok(ws, "edit", "DEMO-0001", "--description", text + "\n\nMore.")
    body = card(ws, "DEMO-0001")["_body"]
    assert body.count("More.") == 1 and body.count("### Acceptance criteria") == 1
    assert body.count("\n## Acceptance criteria\n") == 1


def _rewrite_body(ws, cid, old, new):
    path = card_file(ws, cid)
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_edit_without_description_section_fails_and_leaves_card(ws):
    ok(ws, "new", "feature", "F", "--body", "Old.")
    _rewrite_body(ws, "DEMO-0001", "## Description", "## Summary")
    before = card_file(ws, "DEMO-0001").read_text(encoding="utf-8")
    assert "no '## Description' section" in fails(ws, "edit", "DEMO-0001", "--description", "New.")
    assert card_file(ws, "DEMO-0001").read_text(encoding="utf-8") == before


def test_edit_without_heading_after_description_fails_and_leaves_card(ws):
    ok(ws, "new", "feature", "F", "--body", "Old.")
    path = card_file(ws, "DEMO-0001")
    text = path.read_text(encoding="utf-8")
    path.write_text(text[:text.index("## Acceptance criteria")], encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert "no Mako heading after it" in fails(ws, "edit", "DEMO-0001", "--description", "New.")
    assert path.read_text(encoding="utf-8") == before


def test_title_edit_falls_back_to_first_h1(ws):
    ok(ws, "new", "feature", "Thing", "--body", "Old.")
    _rewrite_body(ws, "DEMO-0001", "# Thing\n", "# Thing (hand edited)\n")
    ok(ws, "edit", "DEMO-0001", "--title", "Renamed")
    body = card(ws, "DEMO-0001")["_body"]
    assert "# Renamed\n" in body and "hand edited" not in body
