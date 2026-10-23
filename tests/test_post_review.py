"""post_review: one slot reviews another's uploaded change through the review backend's comment
hook. A stub backend logs the calls, as in test_review_backend.py."""
import json

from helpers import commit, fails, git, mcp_err, mcp_guide, mcp_ok, mcp_tools, ok
from test_review_backend import STUB, calls, use_stub

COMMENTING = STUB.replace("    return {\"text\": f\"stub review of {issue['id']}\", \"ref\": 7}",
                          "    return {\"text\": f\"stub review of {issue['id']}\", \"ref\": 7, \"revision\": 3}") + '''

def comment(ctx, issue, review):
    _log(ctx, "comment", issue, review)
    return {"text": f"stub posted {review['reviewer']}'s review", "url": "http://stub/7"}
'''
assert '"revision": 3' in COMMENTING


def uploaded(ws) -> str:
    """agent-a's DEMO-0001, sent for review: a.txt has 3 lines, b.txt 1. Returns the sha."""
    use_stub(ws, COMMENTING)
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "one\ntwo\nthree\n")
    sha = commit(ws, "agent-a", "b.txt", "b")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    return sha


def post_err(ws, slot="agent-b", **args) -> str:
    return mcp_err(ws, slot, "post_review", **{"id": "DEMO-0001", "message": "hm", **args})


def test_offered_to_workers_only_when_the_backend_takes_comments(ws):
    use_stub(ws)
    assert "post_review" not in mcp_tools(ws, "agent-b")
    assert "Reviewing each other's issues" not in mcp_guide(ws, "agent-b")
    use_stub(ws, COMMENTING)
    assert "post_review" in mcp_tools(ws, "agent-b")
    assert "post_review" not in mcp_tools(ws, "orchestrator")
    guide = mcp_guide(ws, "agent-b")
    assert "Reviewing each other's issues" in guide and "`mako review <ID> --comment" in guide
    assert "Reviewing each other's issues" not in mcp_guide(ws, "orchestrator")


def test_a_slot_posts_to_the_uploaded_revision(ws):
    sha = uploaded(ws)
    status = json.loads(mcp_ok(ws, "agent-b", "review_status", id="DEMO-0001"))
    assert status["sha"] == sha and status["author"] == "agent-a"
    assert status["parent"] == git(ws, "rev-parse", "main")
    assert "[agent-a] feature/DEMO-0001-reviewed  [in review]" in mcp_ok(ws, "agent-b", "list_issues")

    out = mcp_ok(ws, "agent-b", "post_review", id="DEMO-0001", sha=sha[:8], message=" Looks fine. ", score=1,
                 comments=[{"path": "./a.txt", "line": 3, "message": "typo"},
                           {"path": "b.txt", "message": "needs a newline"}])
    assert out.startswith("stub posted agent-b's review\nTell the user your review of DEMO-0001 is posted")
    _, issue, review = calls(ws)[-1]
    assert issue == {"id": "DEMO-0001", "title": "Reviewed", "branch": "feature/DEMO-0001-reviewed",
                     "agent": "agent-a", "sha": sha, "ref": 7, "revision": 3}
    assert review == {"reviewer": "agent-b", "message": "Looks fine.", "score": 1,
                      "comments": [{"path": "a.txt", "line": 3, "message": "typo"},
                                   {"path": "b.txt", "line": None, "message": "needs a newline"}]}

    commit(ws, "agent-a", "c.txt", "c\n")  # not uploaded yet: no longer waiting for a reviewer
    assert "[in review]" not in mcp_ok(ws, "agent-b", "list_issues")


def test_refusals(ws):
    ok(ws, "new", "feature", "Other")
    use_stub(ws, COMMENTING)
    assert "not in progress" in post_err(ws, sha="x")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "one\ntwo\nthree\n")
    assert "hasn't called request_review" in post_err(ws, sha="x")
    sha = commit(ws, "agent-a", "b.txt", "b")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")

    assert "only worker slots" in post_err(ws, "orchestrator", sha=sha)
    assert "your own claim" in post_err(ws, "agent-a", sha=sha)
    assert "out of range" in post_err(ws, sha=sha, score=2)
    assert "nope.txt is not changed by DEMO-0001" in post_err(
        ws, sha=sha, comments=[{"path": "nope.txt", "line": 1, "message": "x"}])
    err = post_err(ws, sha=sha, comments=[{"path": "a.txt", "line": 4, "message": "x"}])
    assert "a.txt has 3 lines" in err and "no line 4" in err
    assert "b.txt has 1 line " in post_err(ws, sha=sha, comments=[{"path": "b.txt", "line": 2, "message": "x"}])
    assert "bad line 0" in post_err(ws, sha=sha, comments=[{"path": "a.txt", "line": 0, "message": "x"}])
    assert "needs a path and a message" in post_err(ws, sha=sha, comments=[{"path": "a.txt", "line": 1}])

    commit(ws, "agent-a", "a.txt", "one\n")
    mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    new = git(ws, "rev-parse", "feature/DEMO-0001-other")
    err = post_err(ws, sha=sha)
    assert f"newer upload ({new[:7]})" in err and "call review_status again" in err
    assert "newer upload" in post_err(ws, sha="not-a-sha")
    assert not [c for c in calls(ws) if c[0] == "comment"]  # nothing refused reached the backend


def test_files_scope_a_partial_review(ws):
    sha = uploaded(ws)
    assert "can't score +1" in post_err(ws, sha=sha, score=1, files=["a.txt"])
    assert "outside the files you reviewed (a.txt)" in post_err(
        ws, sha=sha, files=["a.txt"], comments=[{"path": "b.txt", "message": "x"}])
    assert "zzz.txt is not changed" in post_err(ws, sha=sha, files=["zzz.txt"])
    mcp_ok(ws, "agent-b", "post_review", id="DEMO-0001", sha=sha, message="Rename it.", score=-1,
           files=["a.txt"], comments=[{"path": "a.txt", "line": 1, "message": "x"}])
    review = calls(ws)[-1][2]
    assert review["message"] == "Reviewed only: a.txt\n\nRename it." and review["score"] == -1


def test_cli_posts_as_human_or_as_the_slot(ws, tmp_path):
    sha = uploaded(ws)
    extra = tmp_path / "comments.json"
    extra.write_text(json.dumps([{"path": "a.txt", "line": 2, "message": "from file"}]), encoding="utf-8")
    out = ok(ws, "review", "DEMO-0001", "--comment", "Approved by me.", "--score", "2",
             "--inline", "a.txt:1=first line", "--inline", "b.txt=whole file", "--comments-file", str(extra))
    assert "stub posted human's review" in out
    _, issue, review = calls(ws)[-1]
    assert issue["sha"] == sha and (review["reviewer"], review["score"]) == ("human", 2)
    assert review["comments"] == [{"path": "a.txt", "line": 1, "message": "first line"},
                                  {"path": "b.txt", "line": None, "message": "whole file"},
                                  {"path": "a.txt", "line": 2, "message": "from file"}]

    assert "out of range" in fails(ws, "review", "DEMO-0001", "--comment", "x", "--score", "2", "--agent", "agent-b")
    assert "your own claim" in fails(ws, "review", "DEMO-0001", "--comment", "x", "--agent", "agent-a")
    assert "only worker slots" in fails(ws, "review", "DEMO-0001", "--comment", "x", "--agent", "orchestrator")
    assert "bad --inline" in fails(ws, "review", "DEMO-0001", "--comment", "x", "--inline", "a.txt:1")
    ok(ws, "review", "DEMO-0001", "--comment", "Partial.", "--files", "a.txt, b.txt", "--sha", sha,
       "--agent", "agent-b")
    assert calls(ws)[-1][2]["message"] == "Reviewed only: a.txt, b.txt\n\nPartial."
