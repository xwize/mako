"""Cards moved by hand: card files moved between kanban/ folders, and human cards."""
import shutil

from helpers import (board, branch_exists, card, commit, drag, fails, git, mcp_err, mcp_ok, move_file,
                     ok, type_card)


def git_state(ws):
    return git(ws, "rev-parse", "main"), git(ws, "branch", "--list")


# --- file moves on normal cards ----------------------------------------------------

def test_file_into_discarded_discards(ws):
    ok(ws, "new", "feature", "Doomed")
    move_file(ws, "DEMO-0001", "discarded")
    assert "kanban: DEMO-0001 moved to kanban/discarded: now Discarded" in ok(ws, "list")
    c = card(ws, "DEMO-0001")
    assert (c["_folder"], c["status"], c["discarded"]) == ("discarded", "discarded", "2026-01-01 00:00")
    assert "## Discarded (human, 2026-01-01 00:00)\n\nMoved to kanban/discarded by hand." in c["_body"]
    assert board(ws)["backlog"] == []


def test_discarding_in_progress_by_file_frees_the_slot_and_warns(ws):
    ok(ws, "new", "feature", "Doomed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    move_file(ws, "DEMO-0001", "discarded")
    out = ok(ws, "list")
    assert "branch feature/DEMO-0001-doomed was kept" in out
    assert card(ws, "DEMO-0001")["status"] == "discarded"
    assert "agent-a    idle" in ok(ws, "status")
    assert branch_exists(ws, "feature/DEMO-0001-doomed")


def test_refused_discard_snaps_back(ws):
    ok(ws, "new", "feature", "Busy")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    (ws.worktree("agent-a") / "wip.txt").write_text("wip\n", encoding="utf-8")
    move_file(ws, "DEMO-0001", "discarded")
    out = ok(ws, "list")
    assert "kanban: can't move DEMO-0001 to kanban/discarded: agent-a's worktree has uncommitted changes" in out
    assert "(moved it back to kanban/open)" in out
    c = card(ws, "DEMO-0001")
    assert (c["_folder"], c["status"], c["claimed_by"]) == ("open", "in-progress", "agent-a")


def test_file_into_open_reopens(ws):
    ok(ws, "new", "feature", "Closed one")
    ok(ws, "new", "feature", "Discarded one")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    ok(ws, "close", "DEMO-0001")
    ok(ws, "discard", "DEMO-0002")
    move_file(ws, "DEMO-0001", "open")
    move_file(ws, "DEMO-0002", "open")
    out = ok(ws, "list")
    for cid in ("DEMO-0001", "DEMO-0002"):
        assert f"kanban: {cid} moved to kanban/open: now Backlog" in out
        c = card(ws, cid)
        assert (c["_folder"], c["status"], c["closed"], c["discarded"]) == ("open", "backlog", "", "")
        assert "Moved to kanban/open by hand." in c["_body"]
    assert sorted(board(ws)["backlog"]) == ["DEMO-0001", "DEMO-0002"]


def test_file_into_closed_is_refused(ws):
    ok(ws, "new", "feature", "Claimed")
    ok(ws, "new", "feature", "Backlog")
    ok(ws, "new", "feature", "Discarded")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    ok(ws, "discard", "DEMO-0003")
    for cid in ("DEMO-0001", "DEMO-0002", "DEMO-0003"):
        move_file(ws, cid, "closed")
    out = ok(ws, "list")
    assert ("can't move DEMO-0001 to kanban/closed: DEMO-0001 can only be closed by an agent "
            "(close_issue / mako close) (moved it back to kanban/open)") in out
    assert "can't move DEMO-0002 to kanban/closed: a Backlog card can't move to Done" in out
    assert "can't move DEMO-0003 to kanban/closed: DEMO-0003 is discarded; reopen it first" in out
    assert [card(ws, c)["_folder"] for c in ("DEMO-0001", "DEMO-0002", "DEMO-0003")] == \
        ["open", "open", "discarded"]
    assert card(ws, "DEMO-0001")["status"] == "in-progress"
    assert not (ws.repo / "a.txt").exists()


def test_done_file_into_discarded_is_refused(ws):
    ok(ws, "new", "feature", "Done")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    ok(ws, "close", "DEMO-0001")
    move_file(ws, "DEMO-0001", "discarded")
    out = ok(ws, "list")
    assert "can't move DEMO-0001 to kanban/discarded: DEMO-0001 is closed; it can't be discarded" in out
    assert card(ws, "DEMO-0001")["_folder"] == "closed"


def test_file_move_wins_over_a_board_drag(ws):
    ok(ws, "new", "feature", "One")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    drag(ws, "DEMO-0001", "backlog")  # would release it
    move_file(ws, "DEMO-0001", "discarded")
    out = ok(ws, "list")
    assert "moved to kanban/discarded" in out and "moved to Backlog" not in out
    assert card(ws, "DEMO-0001")["status"] == "discarded"
    assert board(ws) == {"backlog": [], "in-progress": [], "done": []}


def test_card_in_two_folders_is_left_alone(ws):
    ok(ws, "new", "feature", "Twin")
    src = ws.root / "kanban" / "open" / "DEMO-0001.md"
    shutil.copy(src, ws.root / "kanban" / "closed" / "DEMO-0001.md")
    out = ok(ws, "list")
    assert "DEMO-0001 is in more than one folder (kanban/open, kanban/closed); neither copy was acted on" in out
    assert src.is_file() and (ws.root / "kanban" / "closed" / "DEMO-0001.md").is_file()


# --- human cards ---------------------------------------------------------------------

def test_human_card_moves_freely_on_the_board(ws):
    ok(ws, "new", "feature", "Set up the API key", "--human")
    assert card(ws, "DEMO-0001")["human"] == "true"
    assert "#feature #human" in (ws.root / "kanban" / "board.md").read_text(encoding="utf-8")
    before = git_state(ws)
    for column, folder in (("in-progress", "open"), ("done", "closed"), ("backlog", "open"),
                           ("done", "closed"), ("in-progress", "open")):
        drag(ws, "DEMO-0001", column)
        ok(ws, "list")
        c = card(ws, "DEMO-0001")
        assert (c["status"], c["_folder"]) == (column, folder)
        assert board(ws)[column] == ["DEMO-0001"]
        assert c["closed"] == ("2026-01-01 00:00" if column == "done" else "")
    assert git_state(ws) == before
    assert not list((ws.root / ".mako" / "locks").iterdir())


def test_human_card_moves_freely_between_folders(ws):
    ok(ws, "new", "feature", "Mine", "--human")
    before = git_state(ws)
    for folder, status in (("closed", "done"), ("discarded", "discarded"), ("closed", "done"),
                           ("open", "backlog"), ("discarded", "discarded"), ("open", "backlog")):
        move_file(ws, "DEMO-0001", folder)
        ok(ws, "list")
        c = card(ws, "DEMO-0001")
        assert (c["status"], c["_folder"]) == (status, folder)
    assert git_state(ws) == before


def test_agents_cannot_work_on_human_cards(ws):
    ok(ws, "new", "feature", "Mine", "--human")
    ok(ws, "new", "feature", "Theirs")
    assert "human task" in mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "human task" in fails(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    assert "Claimed DEMO-0002" in mcp_ok(ws, "agent-a", "claim_next")
    drag(ws, "DEMO-0001", "in-progress")
    ok(ws, "list")
    assert "agents can't close it" in mcp_err(ws, "agent-b", "close_issue", id="DEMO-0001", note="x")
    assert "agents can't release it" in mcp_err(ws, "agent-b", "release_issue", id="DEMO-0001")
    listing = mcp_ok(ws, "orchestrator", "list_issues")
    assert "DEMO-0001  feature  Mine  [human]" in listing


def test_agent_card_waits_for_a_human_card(ws):
    text = mcp_ok(ws, "orchestrator", "create_issue", type="feature", title="Get a key", human=True)
    assert "Created DEMO-0001" in text
    ok(ws, "new", "feature", "Use the key", "--depends", "DEMO-0001")
    assert "blocked by DEMO-0001" in mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0002")
    drag(ws, "DEMO-0001", "done")
    assert "Claimed DEMO-0002" in mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_typed_human_card_and_edit_marker(ws):
    type_card(ws, "in-progress", "Normal typed card")
    type_card(ws, "in-progress", "Call the bank #human")  # typed above it, so it becomes DEMO-0001
    ok(ws, "list")
    c = card(ws, "DEMO-0001")
    assert (c["title"], c["human"], c["status"]) == ("Call the bank", "true", "in-progress")
    assert card(ws, "DEMO-0002")["status"] == "backlog"  # only agents put normal cards In Progress

    ok(ws, "edit", "DEMO-0002", "--human")
    assert card(ws, "DEMO-0002")["human"] == "true"
    ok(ws, "edit", "DEMO-0002", "--no-human")
    assert "human" not in card(ws, "DEMO-0002")
    mcp_ok(ws, "orchestrator", "edit_issue", id="DEMO-0002", human=True)
    assert card(ws, "DEMO-0002")["human"] == "true"
