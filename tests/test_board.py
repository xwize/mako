"""Board sync: what the user does in Obsidian is picked up by the next command."""
from helpers import board, branch_exists, card, commit, drag, edit_frontmatter, git, mcp_ok, ok, type_card


def test_drag_in_progress_to_backlog_releases(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    drag(ws, "DEMO-0001", "backlog")
    assert "board: DEMO-0001 moved to Backlog" in ok(ws, "list")
    c = card(ws, "DEMO-0001")
    assert (c["status"], c["claimed_by"]) == ("backlog", "")
    assert branch_exists(ws, "feature/DEMO-0001-one")
    assert board(ws)["backlog"] == ["DEMO-0001"]


def test_drag_in_progress_to_done_closes_and_merges(ws):
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    drag(ws, "DEMO-0001", "done")
    assert "board: DEMO-0001 moved to Done" in ok(ws, "list")
    assert card(ws, "DEMO-0001")["status"] == "done"
    assert (ws.repo / "a.txt").is_file()


def test_drag_backlog_to_in_progress_snaps_back(ws):
    ok(ws, "new", "feature", "One")
    drag(ws, "DEMO-0001", "in-progress")
    out = ok(ws, "list")
    assert "can't move DEMO-0001 to In Progress" in out and "moved it back" in out
    assert card(ws, "DEMO-0001")["status"] == "backlog"
    assert board(ws) == {"backlog": ["DEMO-0001"], "in-progress": [], "done": []}


def test_drag_backlog_to_done_snaps_back(ws):
    ok(ws, "new", "feature", "One")
    drag(ws, "DEMO-0001", "done")
    assert "can't move DEMO-0001 to Done" in ok(ws, "list")
    assert board(ws)["backlog"] == ["DEMO-0001"]


def test_drag_done_to_backlog_reopens(ws):
    ok(ws, "new", "feature", "One")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    ok(ws, "close", "DEMO-0001")
    drag(ws, "DEMO-0001", "backlog")
    ok(ws, "list")
    assert card(ws, "DEMO-0001")["_folder"] == "open"
    assert card(ws, "DEMO-0001")["status"] == "backlog"


def test_typed_card_becomes_a_real_card(ws):
    type_card(ws, "backlog", "bug: Typed on the board")
    out = ok(ws, "list")
    assert "board: created DEMO-0001 from new card 'Typed on the board'" in out
    c = card(ws, "DEMO-0001")
    assert (c["type"], c["title"], c["_folder"]) == ("bug", "Typed on the board", "open")
    assert board(ws)["backlog"] == ["DEMO-0001"]
    # No prefix and no tag means feature. A #type tag sets the type and is dropped from
    # the title; a prefix beats a tag, and the first of several tags wins.
    type_card(ws, "backlog", "Plain idea")
    type_card(ws, "backlog", "Look into it #spike")
    type_card(ws, "backlog", "Crash on start #bug")
    type_card(ws, "backlog", "spike: Prefix wins #bug")
    type_card(ws, "backlog", "Tag #bug in the middle")
    type_card(ws, "backlog", "Two tags #spike #bug")
    ok(ws, "list")
    types = {card(ws, f"DEMO-000{i}")["title"]: card(ws, f"DEMO-000{i}")["type"] for i in range(2, 8)}
    assert types == {"Plain idea": "feature", "Look into it": "spike", "Crash on start": "bug",
                     "Prefix wins": "spike", "Tag in the middle": "bug", "Two tags": "spike"}


def test_board_order_is_kept(ws):
    for t in ("A", "B", "C"):
        ok(ws, "new", "feature", t)
    assert board(ws)["backlog"] == ["DEMO-0001", "DEMO-0002", "DEMO-0003"]
    drag(ws, "DEMO-0003", "backlog")  # to the top of the same column
    ok(ws, "list")
    assert board(ws)["backlog"] == ["DEMO-0003", "DEMO-0001", "DEMO-0002"]


def test_hand_edited_frontmatter_is_picked_up(ws):
    ok(ws, "new", "feature", "Old title")
    edit_frontmatter(ws, "DEMO-0001", title="New title")
    assert "New title" in ok(ws, "list")
    assert "[[DEMO-0001]] New title" in (ws.root / "kanban" / "board.md").read_text(encoding="utf-8")
    assert git(ws, "status", "--porcelain") == ""
