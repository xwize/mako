"""Review backends: "review": {"backend": ..} loads .mako/templates/review/<name>.py and Mako calls
its hooks. A stub backend that logs its calls stands in for a real one."""
import json

from helpers import card, commit, fails, mcp_err, mcp_guide, mcp_ok, mcp_tools, ok, set_config

STUB = '''
import json
from pathlib import Path


class ReviewError(Exception):
    pass


def _log(ctx, *call):
    if ctx["options"].get("down"):
        raise ReviewError("stub is down")
    with open(Path(ctx["root"]) / "stub.log", "a", encoding="utf-8") as out:
        out.write(json.dumps(call) + "\\n")


def setup(ctx):
    _log(ctx, "setup", ctx["agents"], ctx["base"])
    return "stub is set up"


def request(ctx, issue):
    _log(ctx, "request", issue["id"], issue["agent"], issue["branch"], issue["sha"])
    return {"text": f"stub review of {issue['id']}", "ref": 7}


def status(ctx, issue_id):
    _log(ctx, "status", issue_id)
    return {"issue": issue_id, "state": "pending"}


def finish(ctx, issue, event, outcome, note):
    _log(ctx, "finish", issue["id"], issue["ref"], event, outcome, note)
    return {"transcript": f"# {issue['id']} {event}\\n"}
'''


def use_stub(ws, source=STUB, **options):
    (ws.root / ".mako" / "templates" / "review" / "stub.py").write_text(source, encoding="utf-8")
    set_config(ws, review={"backend": "stub", **options})


def calls(ws) -> list:
    path = ws.root / "stub.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def reviewed(ws, slot="agent-a"):
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, slot, "claim_issue", id="DEMO-0001")
    sha = commit(ws, slot, "a.txt", "a\n")
    return sha, mcp_ok(ws, slot, "request_review", id="DEMO-0001")


def test_without_a_review_block_no_backend_is_loaded(ws):
    tools = mcp_tools(ws, "agent-a")
    assert "review_status" not in tools and "request_review" not in tools
    assert "needs a review backend" in mcp_err(ws, "agent-a", "review_status", id="DEMO-0001")
    assert "no review backend with a setup step" in fails(ws, "review", "setup")


def test_request_review_calls_the_backend_and_gates_close(ws):
    use_stub(ws)
    sha, out = reviewed(ws)
    assert out.startswith("stub review of DEMO-0001\nEnd your turn and wait.")
    assert "call review_status" in out
    assert calls(ws) == [["request", "DEMO-0001", "agent-a", "feature/DEMO-0001-reviewed", sha]]
    assert card(ws, "DEMO-0001")["reviews"] == "1"
    commit(ws, "agent-a", "b.txt", "b\n")
    assert "call `request_review` first" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def kanban_files(ws) -> list:
    kanban = ws.root / "kanban"
    return sorted(p.relative_to(kanban).as_posix() for p in kanban.rglob("*") if p.is_file())


def test_init_lists_review_transcripts_to_and_never_changes_it(ws):
    path = ws.root / ".mako" / "config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["review_transcripts_to"] == ""
    set_config(ws, review_transcripts_to="reviews")
    ok(ws, "init")
    assert json.loads(path.read_text(encoding="utf-8"))["review_transcripts_to"] == "reviews"


def test_close_finishes_the_review_and_keeps_no_transcript_by_default(ws):
    use_stub(ws)
    reviewed(ws)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="All done.")
    assert "transcript" not in out
    assert calls(ws)[-1] == ["finish", "DEMO-0001", 7, "close", "merged into main", "All done."]
    assert kanban_files(ws) == ["board.md", "closed/DEMO-0001.md"]


def test_close_keeps_the_transcript_in_review_transcripts_to(ws):
    use_stub(ws)
    set_config(ws, review_transcripts_to="notes/reviews")
    reviewed(ws)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="All done.")
    target = ws.root / "notes" / "reviews" / "review_DEMO-0001.md"
    assert f"wrote the review transcript to {target}" in out
    assert calls(ws)[-1] == ["finish", "DEMO-0001", 7, "close", "merged into main", "All done."]
    assert target.read_text(encoding="utf-8") == "# DEMO-0001 close\n"
    assert kanban_files(ws) == ["board.md", "closed/DEMO-0001.md"]


def test_release_and_discard_finish_without_a_transcript(ws):
    use_stub(ws)
    reviewed(ws)
    mcp_ok(ws, "agent-a", "release_issue", id="DEMO-0001", note="later")
    assert calls(ws)[-1] == ["finish", "DEMO-0001", 7, "release", "released, back in the Backlog", "later"]
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    mcp_ok(ws, "agent-a", "discard_issue", id="DEMO-0001", note="no")
    assert calls(ws)[-1] == ["finish", "DEMO-0001", 7, "discard", "discarded, not merged", "no"]
    assert not (ws.root / "kanban" / "closed" / "review_DEMO-0001.md").exists()


def test_an_issue_never_sent_is_not_finished(ws):
    use_stub(ws)
    ok(ws, "new", "feature", "Plain")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    ok(ws, "close", "DEMO-0001", "--note", "by hand")
    assert calls(ws) == []


def test_status_and_setup(ws):
    use_stub(ws)
    ok(ws, "new", "feature", "Reviewed")
    assert {"request_review", "review_status"} <= set(mcp_tools(ws, "agent-a"))
    assert "`mako review <ID> --status`" in mcp_guide(ws, "agent-a")
    assert json.loads(mcp_ok(ws, "agent-a", "review_status", id="DEMO-0001")) == \
        {"issue": "DEMO-0001", "state": "pending"}
    assert json.loads(ok(ws, "review", "DEMO-0001", "--status"))["state"] == "pending"
    assert ok(ws, "review", "setup").strip() == "stub is set up"
    assert calls(ws)[-1] == ["setup", ["agent-a", "agent-b"], "main"]


def test_hooks_are_optional_except_request(ws):
    use_stub(ws, "class ReviewError(Exception):\n    pass\n\n\ndef request(ctx, issue):\n    return None\n")
    assert "review_status" not in mcp_tools(ws, "agent-a")
    _, out = reviewed(ws)
    assert out.startswith("Sent DEMO-0001 for review.")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_backend_down_records_nothing_and_close_still_works(ws):
    use_stub(ws, down=True)
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    err = mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    assert "stub is down. No review was recorded; call request_review again" in err
    assert card(ws, "DEMO-0001")["reviews"] == "0"

    use_stub(ws)  # up: review, then down again for the close
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    use_stub(ws, down=True)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert "warning: stub is down. The review of DEMO-0001 was left as it is" in out
    assert card(ws, "DEMO-0001")["status"] == "done"


def test_unknown_or_bad_backend_is_refused(ws):
    set_config(ws, review={"backend": "phabricator"})
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "unknown review backend 'phabricator'" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    set_config(ws, review={"backend": "../x"})
    assert "bad review backend name" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
