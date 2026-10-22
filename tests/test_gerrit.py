"""The Gerrit review backend (src/templates/review/gerrit.py): the parts that need no Gerrit.
Talking to a real Gerrit is tested by hand in a testing/ deployment."""
import importlib.util
import re
import socket

import pytest
from helpers import SRC, card, commit, git, mcp_err, mcp_instructions, mcp_ok, ok, set_config

CLIENT = SRC / "templates" / "review" / "gerrit.py"
spec = importlib.util.spec_from_file_location("gerrit_backend", CLIENT)
gerrit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gerrit)


def dead_url() -> str:
    """A URL nothing answers at."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_the_backend_knows_nothing_about_hosting():
    text = CLIENT.read_text(encoding="utf-8").lower()
    for word in ("docker", "podman", "container", "wsl"):
        assert word not in text, word


def test_mako_knows_nothing_about_gerrit():
    assert "gerrit" not in (SRC / "mako.py").read_text(encoding="utf-8").lower()


def test_change_id_is_stable_per_issue():
    assert gerrit.change_id("DEMO-0001") == gerrit.change_id("DEMO-0001")
    assert gerrit.change_id("DEMO-0001") != gerrit.change_id("DEMO-0002")
    assert re.fullmatch(r"I[0-9a-f]{40}", gerrit.change_id("DEMO-0001"))


@pytest.mark.parametrize("status,scores,state", [
    ("NEW", [], "pending"), ("NEW", [1], "pending"), ("NEW", [2], "approved"),
    ("NEW", [2, -1], "changes_requested"), ("MERGED", [2], "merged"), ("ABANDONED", [], "abandoned")])
def test_review_state(status, scores, state):
    detail = {"status": status, "labels": {"Code-Review": {"all": [{"value": s} for s in scores] + [{}]}}}
    assert gerrit.review_state(detail) == state


def test_squash_is_one_commit_and_leaves_the_branch_alone(ws, monkeypatch):
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
        monkeypatch.setenv(key, ws.env[key])
    ok(ws, "new", "feature", "Squashed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    branch = card(ws, "DEMO-0001")["branch"]
    commit(ws, "agent-a", "a.txt", "a\n", "first")
    head = commit(ws, "agent-a", "b.txt", "b\n", "second")
    g = gerrit.Gerrit("http://gerrit.invalid", ws.root / "state", "demo")

    squash = g.squash(ws.repo, "DEMO-0001", "agent-a", branch, "main", "Squashed")
    assert g.squash(ws.repo, "DEMO-0001", "agent-a", branch, "main", "Squashed") == squash  # no empty patch set
    assert git(ws, "rev-parse", branch) == head  # the agent's history is not rewritten
    assert git(ws, "rev-parse", f"{squash}^") == git(ws, "rev-parse", "main")
    assert git(ws, "rev-parse", f"{squash}^{{tree}}") == git(ws, "rev-parse", f"{branch}^{{tree}}")
    message = git(ws, "log", "-1", "--format=%B", squash)
    assert message.startswith("DEMO-0001: Squashed\n") and "first" in message and "second" in message
    assert message.rstrip().endswith(f"Change-Id: {gerrit.change_id('DEMO-0001')}")
    assert git(ws, "log", "-1", "--format=%ae %ce", squash) == "agent-a@mako.example.com agent-a@mako.example.com"

    git(ws, "revert", "--no-edit", "main..HEAD", cwd=ws.worktree("agent-a"))  # changes nothing overall
    with pytest.raises(gerrit.ReviewError, match="nothing to review"):
        g.squash(ws.repo, "DEMO-0001", "agent-a", branch, "main", "Squashed")


def test_installed_and_offered_to_workers(ws):
    assert (ws.root / ".mako" / "templates" / "review" / "gerrit.py").is_file()
    set_config(ws, review={"backend": "gerrit", "url": dead_url()})
    assert "Reviews happen in Gerrit" in mcp_instructions(ws, "agent-a")
    assert "Reviews happen in Gerrit" not in mcp_instructions(ws, "orchestrator")


def test_unreachable_gerrit_says_what_to_do(ws):
    url = dead_url()
    set_config(ws, review={"backend": "gerrit", "url": url})
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert f"Gerrit not reachable at {url}; start it (e.g. trayit)" in \
        mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")


def test_comment_posts_unresolved_to_the_patch_set_and_score_0_keeps_the_vote(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(gerrit.Gerrit, "api", lambda self, method, path, body=None, as_="admin":
                        sent.append((method, path, body, as_)))
    ctx = {"options": {}, "state_dir": tmp_path, "project": "demo", "workspace_tag": "demo"}
    issue = {"id": "DEMO-0001", "ref": 7, "revision": 3}
    review = {"reviewer": "agent-b", "message": "m", "score": -1,
              "comments": [{"path": "a.txt", "line": 2, "message": "x"},
                           {"path": "a.txt", "line": None, "message": "y"}]}
    out = gerrit.comment(ctx, issue, review)
    assert "patch set 3: Code-Review -1, 2 inline comments" in out["text"]
    assert sent[-1] == ("POST", "/changes/7/revisions/3/review",
                        {"message": "m", "labels": {"Code-Review": -1}, "comments": {"a.txt": [
                            {"message": "x", "unresolved": True, "line": 2},
                            {"message": "y", "unresolved": True}]}}, "agent-b")
    gerrit.comment(ctx, {**issue, "revision": None}, {**review, "score": 0, "comments": []})
    assert sent[-1][1] == "/changes/7/revisions/current/review" and "labels" not in sent[-1][2]
