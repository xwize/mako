"""discard, reopen and release, with their warnings."""
from helpers import board, branch_exists, card, commit, fails, git, mcp_err, mcp_ok, ok


def test_discard_claimed_issue_keeps_branch_and_warns(ws):
    ok(ws, "new", "feature", "Doomed")
    ok(ws, "new", "feature", "Dependent", "--depends", "DEMO-0001")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    main_before = git(ws, "rev-parse", "main")

    text = mcp_ok(ws, "agent-a", "discard_issue", id="DEMO-0001", note="Not needed.")
    assert "Discarded DEMO-0001" in text
    assert "branch feature/DEMO-0001-doomed was kept" in text
    assert "DEMO-0002 depends on DEMO-0001 and is now blocked; run `mako undepend DEMO-0002 DEMO-0001`" in text

    c = card(ws, "DEMO-0001")
    assert (c["_folder"], c["status"], c["claimed_by"]) == ("discarded", "discarded", "")
    assert "Not needed." in c["_body"]
    assert "DEMO-0001" not in sum(board(ws).values(), [])
    assert branch_exists(ws, "feature/DEMO-0001-doomed")
    assert git(ws, "rev-parse", "main") == main_before
    assert "depends on DEMO-0001: blocked by DEMO-0001 (discarded)" in ok(ws, "show", "DEMO-0002")


def test_discard_refuses_uncommitted_work_unless_forced(ws):
    ok(ws, "new", "feature", "Doomed")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    (ws.worktree("agent-a") / "wip.txt").write_text("wip\n", encoding="utf-8")
    assert "uncommitted changes" in fails(ws, "discard", "DEMO-0001")
    out = ok(ws, "discard", "DEMO-0001", "--force")
    assert "committed agent-a's uncommitted changes" in out
    assert git(ws, "show", "feature/DEMO-0001-doomed:wip.txt") == "wip"


def test_worker_can_discard_only_own_or_unclaimed(ws):
    ok(ws, "new", "feature", "Mine")
    ok(ws, "new", "feature", "Theirs")
    ok(ws, "new", "feature", "Nobody's")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0002")
    assert "claimed by agent-b, not agent-a" in mcp_err(ws, "agent-a", "discard_issue", id="DEMO-0002")
    mcp_ok(ws, "agent-a", "discard_issue", id="DEMO-0003")
    assert card(ws, "DEMO-0003")["status"] == "discarded"


def test_done_cards_cannot_be_discarded(ws):
    ok(ws, "new", "feature", "Done one")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    ok(ws, "close", "DEMO-0001")
    assert "is closed and merged; it can't be discarded" in fails(ws, "discard", "DEMO-0001")


def test_discarded_card_dragged_back_onto_board_is_removed(ws):
    ok(ws, "new", "feature", "Doomed")
    ok(ws, "discard", "DEMO-0001")
    board_md = ws.root / "kanban" / "board.md"
    text = board_md.read_text(encoding="utf-8")
    board_md.write_text(text.replace("## Backlog\n\n", "## Backlog\n\n- [ ] [[DEMO-0001]] Doomed #feature\n", 1),
                        encoding="utf-8")
    assert "DEMO-0001 is discarded, removed from the board" in ok(ws, "list")
    assert card(ws, "DEMO-0001")["status"] == "discarded"
    assert board(ws)["backlog"] == []


def test_new_dependency_on_discarded_issue_is_refused(ws):
    ok(ws, "new", "feature", "Doomed")
    ok(ws, "new", "feature", "Other")
    ok(ws, "discard", "DEMO-0001")
    assert "DEMO-0001 is discarded" in fails(ws, "depend", "DEMO-0002", "DEMO-0001")


def test_reopen_discarded_resumes_kept_branch(ws):
    ok(ws, "new", "feature", "Back again")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    sha = commit(ws, "agent-a", "a.txt", "a\n")
    mcp_ok(ws, "agent-a", "discard_issue", id="DEMO-0001")
    out = ok(ws, "reopen", "DEMO-0001")
    assert "branch feature/DEMO-0001-back-again was kept; the next claim resumes it" in out
    c = card(ws, "DEMO-0001")
    assert (c["_folder"], c["status"], c["discarded"]) == ("open", "backlog", "")
    assert board(ws)["backlog"] == ["DEMO-0001"]
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0001")
    assert git(ws, "rev-parse", "HEAD", cwd=ws.worktree("agent-b")) == sha


def test_reopen_closed_issue_warns_about_started_dependents(ws):
    ok(ws, "new", "feature", "Base")
    ok(ws, "new", "feature", "After", "--depends", "DEMO-0001")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    ok(ws, "close", "DEMO-0001")
    ok(ws, "claim", "DEMO-0002", "--agent", "agent-b")
    out = ok(ws, "reopen", "DEMO-0001")
    assert "issues that depend on DEMO-0001 have already started: DEMO-0002 (In Progress)" in out
    assert card(ws, "DEMO-0001")["_folder"] == "open"
    assert "is not closed or discarded" in fails(ws, "reopen", "DEMO-0002")
