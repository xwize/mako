"""The Gerrit review backend: the client (src/templates/gerrit/gerrit.py) against a fake Gerrit,
and Mako's side (config, tools, guide, close/release/discard, an unreachable Gerrit).
Pushing needs a real Gerrit and is tested by hand in testing/."""
import importlib.util
import json
import re
import socket

import pytest
from fake_gerrit import FakeGerrit
from helpers import (SRC, card, commit, fails, git, mcp_err, mcp_instructions, mcp_ok, mcp_tools, ok,
                     set_config)

CLIENT = SRC / "templates" / "gerrit" / "gerrit.py"
spec = importlib.util.spec_from_file_location("gerrit_client", CLIENT)
gerrit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gerrit)


@pytest.fixture
def fake():
    server = FakeGerrit()
    yield server
    server.close()


def dead_url() -> str:
    """A URL nothing answers at."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def client(url, tmp_path, project="demo") -> "gerrit.Gerrit":
    g = gerrit.Gerrit(url, tmp_path / "state", project, "mako-test")
    g.sync_base = lambda repo, base: None  # git over HTTP: not in the fake
    return g


def use_gerrit(ws, url):
    set_config(ws, review={"backend": "gerrit", "url": url})


def claimed_with_commit(ws, slot="agent-a"):
    ok(ws, "new", "feature", "Reviewed")
    mcp_ok(ws, slot, "claim_issue", id="DEMO-0001")
    return commit(ws, slot, "a.txt", "a\n")


def fake_review_recorded(ws, sha, change=1):
    """What request_review leaves in the lock after a push (the fake can't take pushes)."""
    path = ws.root / ".mako" / "locks" / "DEMO-0001" / "owner.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    lock.update(reviewed=sha, gerrit_change=change)
    path.write_text(json.dumps(lock), encoding="utf-8")


# --- the client ------------------------------------------------------------------

def test_client_knows_nothing_about_hosting():
    text = CLIENT.read_text(encoding="utf-8").lower()
    for word in ("docker", "podman", "container", "wsl", "localhost", "127.0.0.1", "8080"):
        assert word not in text, word


def test_change_id_is_stable_per_issue():
    assert gerrit.change_id("DEMO-0001") == gerrit.change_id("DEMO-0001")
    assert gerrit.change_id("DEMO-0001") != gerrit.change_id("DEMO-0002")
    assert re.fullmatch(r"I[0-9a-f]{40}", gerrit.change_id("DEMO-0001"))


@pytest.mark.parametrize("status,scores,state", [
    ("NEW", [], "pending"), ("NEW", [1], "pending"), ("NEW", [2], "approved"),
    ("NEW", [2, -1], "changes_requested"), ("NEW", [-2], "changes_requested"),
    ("MERGED", [2], "merged"), ("ABANDONED", [], "abandoned")])
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
    assert message.startswith("DEMO-0001: Squashed\n")
    assert "second" in message and "first" in message
    assert message.rstrip().endswith(f"Change-Id: {gerrit.change_id('DEMO-0001')}")
    assert git(ws, "log", "-1", "--format=%an <%ae> %cn <%ce>", squash) == \
        "agent-a <agent-a@mako.example.com> agent-a <agent-a@mako.example.com>"
    assert not list((ws.repo / ".git" / "hooks").glob("commit-msg"))

    git(ws, "revert", "--no-edit", "main..HEAD", cwd=ws.worktree("agent-a"))  # changes nothing overall
    with pytest.raises(gerrit.GerritError, match="nothing to review"):
        g.squash(ws.repo, "DEMO-0001", "agent-a", branch, "main", "Squashed")


def test_setup_creates_accounts_project_and_is_idempotent(fake, tmp_path):
    g = client(fake.url, tmp_path)
    done = g.setup(["human", "agent-a", "agent-b"], tmp_path, "main")
    assert done == ["account admin", "account human", "account agent-a", "account agent-b", "project demo"]
    assert sorted(fake.usernames()) == ["admin", "agent-a", "agent-b", "human"]
    assert fake.accounts[fake.by_name("agent-a")]["emails"] == {"agent-a@mako.example.com"}
    assert fake.by_name("human") in fake.admins
    assert "demo" in fake.projects and fake.access
    saved = json.loads((tmp_path / "state" / "credentials.json").read_text(encoding="utf-8"))
    assert saved["url"] == fake.url and set(saved["accounts"]) == {"admin", "human", "agent-a", "agent-b"}

    again = client(fake.url, tmp_path)
    assert again.setup(["human", "agent-a", "agent-b"], tmp_path, "main") == []
    assert sorted(fake.usernames()) == ["admin", "agent-a", "agent-b", "human"]


def test_setup_resumes_after_a_failure_without_duplicates(fake, tmp_path):
    fake.fail_after_accounts = 2  # admin and human exist, then account creation fails
    with pytest.raises(gerrit.HttpError):
        client(fake.url, tmp_path).setup(["human", "agent-a"], tmp_path, "main")
    fake.fail_after_accounts = None
    client(fake.url, tmp_path).setup(["human", "agent-a"], tmp_path, "main")
    assert sorted(fake.usernames()) == ["admin", "agent-a", "human"]


def test_setup_adopts_accounts_the_gerrit_already_has(fake, tmp_path):
    client(fake.url, tmp_path / "one").setup(["human", "agent-a"], tmp_path, "main")
    client(fake.url, tmp_path / "two").setup(["human", "agent-a"], tmp_path, "main")  # credentials lost
    assert sorted(fake.usernames()) == ["admin", "agent-a", "human"]


def test_a_refused_token_is_replaced(fake, tmp_path):
    g = client(fake.url, tmp_path)
    g.setup(["human"], tmp_path, "main")
    old = g.accounts["human"]["token"]
    fake.accounts[fake.by_name("human")]["tokens"].clear()  # e.g. revoked in the UI
    assert g.api("GET", "/accounts/self", as_="human")["username"] == "human"
    assert g.accounts["human"]["token"] != old


def test_setup_refuses_a_gerrit_without_dev_auth(tmp_path):
    server = FakeGerrit(auth_type="OAUTH")
    try:
        with pytest.raises(gerrit.GerritError, match="DEVELOPMENT_BECOME_ANY_ACCOUNT"):
            client(server.url, tmp_path).setup(["human"], tmp_path, "main")
        assert server.usernames() == ["admin"]
    finally:
        server.close()


def test_unreachable(tmp_path):
    with pytest.raises(gerrit.Unreachable):
        client(dead_url(), tmp_path).setup(["human"], tmp_path, "main")


def test_status_transcript_and_finish(fake, tmp_path):
    g = client(fake.url, tmp_path)
    g.setup(["human", "agent-a"], tmp_path, "main")
    fake.add_change("demo", gerrit.change_id("DEMO-0001"), "DEMO-0001", "agent-a")
    assert g.status("DEMO-0001")["state"] == "pending"
    g.vote("DEMO-0001", "human", -1, "Please rename.\n\nThanks.", {"a.py": [{"line": 3, "message": "typo"}]})

    s = g.status("DEMO-0001")
    assert (s["state"], s["owner"], s["patch_set"]) == ("changes_requested", "agent-a", 1)
    assert s["votes"] == {"Code-Review": {"human": -1}}
    assert s["comments"] == [{"path": "a.py", "line": 3, "patch_set": 1, "author": "human",
                              "message": "typo", "unresolved": False}]
    assert s["url"] == f"{fake.url}/c/demo/+/1"

    assert g.finish("DEMO-0001", "Closed by Mako (merged into main).") == 1
    assert fake.changes[1]["status"] == "ABANDONED"
    assert g.finish("DEMO-0001", "again") is None  # already abandoned: left alone
    text = g.transcript("DEMO-0001")
    assert "owner **agent-a**, abandoned" in text
    assert "- **human** (patch set 1): Please rename.\n\n  Thanks." in text
    assert "- patch set 1, `a.py:3`, **human**: typo" in text
    assert "Closed by Mako (merged into main)." in text

    with pytest.raises(gerrit.GerritError, match="no change for DEMO-0002"):
        g.status("DEMO-0002")


def test_sign_in_link_names_the_change_and_the_human(fake, tmp_path):
    g = client(fake.url, tmp_path)
    g.setup(["human"], tmp_path, "main")
    human = fake.by_name("human")
    assert g.sign_in_url(7) == f"{fake.url}/login/%2Fc%2Fdemo%2F%2B%2F7?account_id={human}"


# --- Mako's side ---------------------------------------------------------------------

def test_without_the_review_block_nothing_changes(ws):
    tools = mcp_tools(ws, "agent-a")
    assert "review_status" not in tools and "request_review" not in tools
    assert "Gerrit" not in mcp_instructions(ws, "agent-a")
    assert "needs the Gerrit review backend" in mcp_err(ws, "agent-a", "review_status", id="DEMO-0001")
    assert not (ws.root / ".mako" / "gerrit").exists()


def test_gerrit_backend_offers_review_status_and_says_so(ws):
    use_gerrit(ws, dead_url())
    assert {"request_review", "review_status"} <= set(mcp_tools(ws, "agent-a"))
    assert "review_status" in mcp_tools(ws, "orchestrator")
    guide = mcp_instructions(ws, "agent-a")
    assert "Reviews happen in Gerrit" in guide and "mako review setup" in guide
    assert "Reviews happen in Gerrit" not in mcp_instructions(ws, "orchestrator")


def test_unknown_backend_is_refused(ws):
    set_config(ws, review={"backend": "phabricator"})
    claimed_with_commit(ws)
    assert "unknown review backend 'phabricator'" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")


def test_unreachable_gerrit_is_a_clear_retryable_error(ws):
    url = dead_url()
    use_gerrit(ws, url)
    sha = claimed_with_commit(ws)
    err = mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    assert f"Gerrit not reachable at {url}; start it (e.g. trayit)" in err
    assert "No review was recorded; call request_review again" in err
    assert card(ws, "DEMO-0001")["reviews"] == "0"
    assert f"no review for {sha[:7]}" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert "Gerrit not reachable" in mcp_err(ws, "agent-a", "review_status", id="DEMO-0001")
    assert "Gerrit not reachable" in fails(ws, "review", "setup")
    assert "Gerrit not reachable" in fails(ws, "review", "DEMO-0001", "--status")


def test_close_with_gerrit_down_still_merges_and_warns(ws):
    use_gerrit(ws, dead_url())
    sha = claimed_with_commit(ws)
    fake_review_recorded(ws, sha)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="done")
    assert "Merged" in out
    assert "warning: Gerrit not reachable" in out and "Change 1 was left as it is" in out
    assert card(ws, "DEMO-0001")["status"] == "done"


def test_close_abandons_the_change_and_writes_the_transcript(ws, fake):
    use_gerrit(ws, fake.url)
    sha = claimed_with_commit(ws)
    fake.add_change("demo", gerrit.change_id("DEMO-0001"), "DEMO-0001", "agent-a")
    fake_review_recorded(ws, sha)
    out = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="All done.")
    assert "wrote the review transcript to kanban/closed/review_DEMO-0001.md" in out
    assert fake.changes[1]["status"] == "ABANDONED"
    assert "Closed by Mako (merged into main).\n\nAll done." in fake.changes[1]["messages"][-1]["message"]
    transcript = (ws.root / "kanban" / "closed" / "review_DEMO-0001.md").read_text(encoding="utf-8")
    assert transcript.startswith("# Review of DEMO-0001")
    assert "DEMO-0002" not in ok(ws, "list", "--all")  # the transcript is not taken for a card
    secrets = json.loads((ws.root / ".mako" / "gerrit" / "credentials.json").read_text(encoding="utf-8"))
    for account in secrets["accounts"].values():
        assert account["token"] not in out


@pytest.mark.parametrize("tool,outcome", [("release_issue", "released, back in the Backlog"),
                                          ("discard_issue", "discarded, not merged")])
def test_release_and_discard_abandon_the_change(ws, fake, tool, outcome):
    use_gerrit(ws, fake.url)
    sha = claimed_with_commit(ws)
    fake.add_change("demo", gerrit.change_id("DEMO-0001"), "DEMO-0001", "agent-a")
    fake_review_recorded(ws, sha)
    mcp_ok(ws, "agent-a", tool, id="DEMO-0001", note="why")
    assert fake.changes[1]["status"] == "ABANDONED"
    assert f"Closed by Mako ({outcome}).\n\nwhy" in fake.changes[1]["messages"][-1]["message"]
    assert not (ws.root / "kanban" / "closed" / "review_DEMO-0001.md").exists()


def test_review_status_cli_and_tool(ws, fake):
    use_gerrit(ws, fake.url)
    ok(ws, "new", "feature", "Reviewed")
    fake.add_change("demo", gerrit.change_id("DEMO-0001"), "DEMO-0001", "agent-a")
    assert json.loads(ok(ws, "review", "DEMO-0001", "--status"))["state"] == "pending"
    assert json.loads(mcp_ok(ws, "agent-a", "review_status", id="DEMO-0001"))["owner"] == "agent-a"


def test_close_without_a_gerrit_review_never_calls_gerrit(ws, fake):
    use_gerrit(ws, fake.url)
    claimed_with_commit(ws)
    ok(ws, "close", "DEMO-0001", "--note", "by hand")
    assert fake.calls == []
