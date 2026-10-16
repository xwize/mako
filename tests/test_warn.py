"""The /clear nudge: a heavy close makes that agent's next claim refuse once."""
import json

from helpers import commit, fails, mcp_err, mcp_ok, ok, set_config
from test_review import flag_command


def finish(ws, cid, minutes_open=0, note="x"):
    """Claim as agent-a at the fixed test time, then close `minutes_open` later."""
    start = ws.env["MAKO_NOW"]
    mcp_ok(ws, "agent-a", "claim_issue", id=cid)
    commit(ws, "agent-a", f"{cid}.txt", "a\n")
    hh, mm = divmod(int(start[-5:-3]) * 60 + int(start[-2:]) + minutes_open, 60)
    ws.env["MAKO_NOW"] = f"{start[:-5]}{hh:02d}:{mm:02d}"
    mcp_ok(ws, "agent-a", "close_issue", id=cid, note=note)


def test_slow_close_refuses_next_claim_once(ws):
    ws.env["MAKO_NOW"] = "2026-01-05 09:00"
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    finish(ws, "DEMO-0001", minutes_open=31)
    refusal = mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0002")
    assert "Unclaimed. Remind the user to run /clear periodically and please try again." in refusal
    assert not (ws.root / ".mako" / "locks" / "DEMO-0002").exists()
    assert "backlog" in ok(ws, "show", "DEMO-0002")
    assert "Claimed DEMO-0002" in mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_flag_is_per_agent_and_covers_claim_next_and_cli(ws):
    ws.env["MAKO_NOW"] = "2026-01-05 09:00"
    for title in ("One", "Two", "Three"):
        ok(ws, "new", "feature", title)
    finish(ws, "DEMO-0001", minutes_open=31)
    assert "Claimed DEMO-0002" in mcp_ok(ws, "agent-b", "claim_next")  # agent-b unaffected
    assert mcp_err(ws, "agent-a", "claim_next")
    assert "Claimed DEMO-0003" in mcp_ok(ws, "agent-a", "claim_next")
    # CLI claim goes through the same check.
    commit(ws, "agent-a", "c.txt", "c\n")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0003", note="x")


def test_exactly_the_limit_is_quick(ws):
    ws.env["MAKO_NOW"] = "2026-01-05 09:00"
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    finish(ws, "DEMO-0001", minutes_open=30)
    assert "Claimed DEMO-0002" in mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_review_heavy_close_refuses_next_claim(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"))
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    for _ in range(3):
        mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert "/clear" in mcp_err(ws, "agent-a", "claim_issue", id="DEMO-0002")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_two_reviews_and_a_quick_close_leave_the_flag_unset(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"))
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    for _ in range(2):
        mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_zero_disables_each_test(ws, tmp_path):
    set_config(ws, review_command=flag_command(tmp_path / "review.flag"),
               warn_after_minutes=0, warn_after_reviews=0)
    ws.env["MAKO_NOW"] = "2026-01-05 09:00"
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    for _ in range(5):
        mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    ws.env["MAKO_NOW"] = "2026-01-06 09:00"
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")


def test_release_and_discard_do_not_flag(ws):
    ws.env["MAKO_NOW"] = "2026-01-05 09:00"
    for title in ("One", "Two", "Three"):
        ok(ws, "new", "feature", title)
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    ws.env["MAKO_NOW"] = "2026-01-05 12:00"
    mcp_ok(ws, "agent-a", "release_issue", id="DEMO-0001", note="x")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0002")
    mcp_ok(ws, "agent-a", "discard_issue", id="DEMO-0002", note="x")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0003")


def test_cli_claim_is_refused_once(ws):
    ws.env["MAKO_NOW"] = "2026-01-05 09:00"
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    finish(ws, "DEMO-0001", minutes_open=60)
    assert "/clear" in fails(ws, "claim", "DEMO-0002", "--agent", "agent-a")
    assert "Claimed DEMO-0002" in ok(ws, "claim", "DEMO-0002", "--agent", "agent-a")


def test_defaults_are_in_new_and_existing_configs(ws):
    path = ws.root / ".mako" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert (config["warn_after_minutes"], config["warn_after_reviews"]) == (30, 2)
    del config["warn_after_minutes"]
    config["warn_after_reviews"] = 5  # a user-set value must survive init
    path.write_text(json.dumps(config), encoding="utf-8")
    ok(ws, "init", "demo")
    config = json.loads(path.read_text(encoding="utf-8"))
    assert (config["warn_after_minutes"], config["warn_after_reviews"]) == (30, 5)
