"""review_command: request_review runs it, close_issue is gated on it, the CLI close is not."""
import sys

from helpers import card, card_file, commit, drag, git, mcp_err, mcp_ok, mcp_tools, ok, set_config, wait_for


def flag_command(flag) -> str:
    """A harmless review command: appends its placeholders to a file outside the worktree."""
    code = f"open(r'{flag}', 'a').write('{{id}} {{branch}} {{base}}')"
    return f'"{sys.executable}" -c "{code}"'


def test_without_review_command_nothing_changes(ws):
    assert "request_review" not in mcp_tools(ws, "agent-a")
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "no review step" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_close_gated_until_review_covers_head(ws, tmp_path):
    flag = tmp_path / "review.flag"
    set_config(ws, review_command=flag_command(flag))
    assert "request_review" in mcp_tools(ws, "agent-a")
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    sha = commit(ws, "agent-a", "a.txt", "a\n")

    assert f"call `request_review` first (no review for {sha[:7]})" in \
        mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")

    text = mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert f"Review opened for DEMO-0001 ({sha[:7]})" in text
    assert wait_for(flag) == "DEMO-0001 feature/DEMO-0001-reviewed main"

    # A new commit since the review: refused again until the next request_review.
    sha2 = commit(ws, "agent-a", "b.txt", "b\n")
    assert f"no review for {sha2[:7]}" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert "Merged" in mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_request_review_guards(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"))
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "claimed by agent-a, not agent-b" in mcp_err(ws, "agent-b", "request_review", id="DEMO-0001")
    (ws.worktree("agent-a") / "dirty.txt").write_text("x", encoding="utf-8")
    assert "uncommitted changes" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")


def test_cli_close_is_not_gated(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"))
    ok(ws, "new", "feature", "Human closes")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "Merged" in ok(ws, "close", "DEMO-0001")
    assert card(ws, "DEMO-0001")["status"] == "done"
    assert git(ws, "log", "-1", "--format=%s", "main") == "Merge DEMO-0001: Human closes"


def test_mako_review_runs_the_command_for_the_human(ws, tmp_path):
    flag = tmp_path / "review.flag"
    set_config(ws, review_command=flag_command(flag))
    ok(ws, "new", "feature", "One")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    ok(ws, "review", "DEMO-0001")
    assert wait_for(flag).startswith("DEMO-0001 ")


def test_reviews_are_counted_on_the_card_and_survive_close(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"))
    ok(ws, "new", "feature", "Counted")
    assert card(ws, "DEMO-0001")["reviews"] == "0"
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    ok(ws, "review", "DEMO-0001")  # same commit again: another round
    assert card(ws, "DEMO-0001")["reviews"] == "2"
    assert "reviews: 2" in ok(ws, "show", "DEMO-0001")
    assert "reviews: 2" in mcp_ok(ws, "agent-a", "get_issue", id="DEMO-0001")
    drag(ws, "DEMO-0001", "in-progress")  # board sync / regeneration keeps it
    ok(ws, "list")
    assert card(ws, "DEMO-0001")["reviews"] == "2"
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    closed = card(ws, "DEMO-0001")
    assert (closed["_folder"], closed["reviews"]) == ("closed", "2")


def test_failed_review_launch_is_not_counted(ws):
    set_config(ws, review_command="x")
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    (ws.worktree("agent-a") / "dirty.txt").write_text("x", encoding="utf-8")
    mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    assert card(ws, "DEMO-0001")["reviews"] == "0"


def test_card_without_reviews_field_counts_as_zero(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"))
    ok(ws, "new", "feature", "Old card")
    path = card_file(ws, "DEMO-0001")
    path.write_text(path.read_text(encoding="utf-8").replace("reviews: 0\n", ""), encoding="utf-8")
    assert "reviews: 0" in ok(ws, "show", "DEMO-0001")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    ok(ws, "review", "DEMO-0001")
    assert card(ws, "DEMO-0001")["reviews"] == "1"
