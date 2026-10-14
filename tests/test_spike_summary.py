"""Spike summary files (.mako_spikes/ on the spike branch) and publish_to."""
import json

from helpers import NOW, card, commit, fails, git, mcp_err, mcp_ok, ok, set_config

BRANCH = "spike/DEMO-0001-try-caching"
SUMMARY = ".mako_spikes/spike_DEMO-0001_try-caching.md"


def summary_on_branch(ws) -> str:
    return git(ws, "show", f"{BRANCH}:{SUMMARY}")


def write_findings(ws, text="## Findings\n\nCaching helps.\n"):
    """The worker fills in the summary body and commits."""
    path = ws.worktree("agent-a") / SUMMARY
    head = path.read_text(encoding="utf-8").split("\n# ", 1)[0]
    commit(ws, "agent-a", SUMMARY, head + "\n# Try caching\n\n" + text, "DEMO-0001: findings")


def test_claim_commits_the_summary_with_frontmatter(ws):
    ok(ws, "new", "spike", "Try caching")
    text = mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert f"summary:  {ws.worktree('agent-a') / SUMMARY}" in text
    assert git(ws, "status", "--porcelain", cwd=ws.worktree("agent-a")) == ""
    assert summary_on_branch(ws).startswith(
        "---\nid: DEMO-0001\ntitle: Try caching\nagent: agent-a\n"
        f'created: "{NOW}"\nbranch: {BRANCH}\nclosed: \n---\n\n# Try caching\n')
    assert git(ws, "log", "-1", "--format=%s", BRANCH) == "DEMO-0001: spike summary"
    assert card(ws, "DEMO-0001")["summary"] == SUMMARY
    # A resumed spike keeps the summary it has.
    write_findings(ws)
    mcp_ok(ws, "agent-a", "release_issue", id="DEMO-0001")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0001")
    assert "Caching helps." in summary_on_branch(ws)
    assert "agent: agent-a" in summary_on_branch(ws)


def test_research_spike_publishes_its_summary_on_close(ws):
    ok(ws, "new", "spike", "Try caching", "--publish-to", "docs/spikes")
    assert card(ws, "DEMO-0001")["publish_to"] == "docs/spikes"
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    write_findings(ws)
    text = mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="Use caching.")
    published = ws.root / "docs" / "spikes" / "spike_DEMO-0001_try-caching.md"
    assert f"published DEMO-0001's summary to {published} (not committed)" in text
    body = published.read_text(encoding="utf-8")
    assert f'closed: "{NOW}"' in body and "Caching helps." in body
    # The branch's copy is filled in too, and nothing was merged.
    assert summary_on_branch(ws) + "\n" == body
    assert not (ws.repo / ".mako_spikes").exists()
    assert f"Summary published to {published}." in card(ws, "DEMO-0001")["_body"]
    assert f"on its branch; published to {published})" in ok(ws, "show", "DEMO-0001")


def test_publishing_into_the_repo_is_not_committed(ws):
    ok(ws, "new", "spike", "Try caching", "--publish-to", "demo/notes")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    write_findings(ws)
    head = git(ws, "rev-parse", "main")
    ok(ws, "close", "DEMO-0001")
    assert (ws.repo / "notes" / "spike_DEMO-0001_try-caching.md").is_file()
    assert git(ws, "status", "--porcelain") == "?? notes/"
    assert git(ws, "rev-parse", "main") == head


def test_prototype_spike_without_publish_to_keeps_the_summary_on_its_branch(ws):
    ok(ws, "new", "spike", "Try caching")
    assert card(ws, "DEMO-0001")["publish_to"] == ""
    assert "not published (publish_to is empty)" in ok(ws, "show", "DEMO-0001")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "proto.py", "print('fast')\n")
    out = ok(ws, "close", "DEMO-0001")
    assert "published" not in out
    assert f'closed: "{NOW}"' in summary_on_branch(ws)
    assert git(ws, "show", f"{BRANCH}:proto.py") == "print('fast')"
    assert not list(ws.root.rglob("spike_DEMO-0001_*.md"))


def test_publish_to_can_change_while_in_progress_but_nothing_else(ws):
    ok(ws, "new", "spike", "Try caching")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    ok(ws, "edit", "DEMO-0001", "--publish-to", "out")
    assert card(ws, "DEMO-0001")["publish_to"] == "out"
    mcp_ok(ws, "agent-a", "edit_issue", id="DEMO-0001", publish_to="research")
    assert card(ws, "DEMO-0001")["publish_to"] == "research"
    assert "in progress" in fails(ws, "edit", "DEMO-0001", "--publish-to", "x", "--title", "New")
    assert "in progress" in mcp_err(ws, "agent-a", "edit_issue", id="DEMO-0001", title="New")
    assert "copied to " + str(ws.root / "research" / "spike_DEMO-0001_try-caching.md") in ok(
        ws, "show", "DEMO-0001")
    write_findings(ws)
    ok(ws, "close", "DEMO-0001")
    assert (ws.root / "research" / "spike_DEMO-0001_try-caching.md").is_file()
    assert not (ws.root / "out").exists()


def test_publish_to_is_for_spikes_only(ws):
    assert "publish_to is for spikes only" in fails(ws, "new", "feature", "F", "--publish-to", "x")
    ok(ws, "new", "feature", "F")
    assert "publish_to is for spikes only" in fails(ws, "edit", "DEMO-0001", "--publish-to", "x")
    assert "publish_to" not in card(ws, "DEMO-0001")


def test_config_default_fills_new_spikes(ws):
    path = ws.root / ".mako" / "config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["spike_publish_to"] == ""
    set_config(ws, spike_publish_to="docs/spikes")
    ok(ws, "new", "spike", "Default")
    ok(ws, "new", "spike", "Override", "--publish-to", "elsewhere")
    ok(ws, "new", "spike", "None", "--publish-to", "")
    ok(ws, "new", "feature", "Feature")
    mcp_ok(ws, "orchestrator", "create_issue", type="spike", title="By agent")
    got = {cid: card(ws, cid).get("publish_to") for cid in
           ("DEMO-0001", "DEMO-0002", "DEMO-0003", "DEMO-0004", "DEMO-0005")}
    assert got == {"DEMO-0001": "docs/spikes", "DEMO-0002": "elsewhere", "DEMO-0003": "",
                   "DEMO-0004": None, "DEMO-0005": "docs/spikes"}
    # Retyping in the Backlog: to spike takes the default, away from spike drops it.
    ok(ws, "edit", "DEMO-0004", "--type", "spike")
    assert card(ws, "DEMO-0004")["publish_to"] == "docs/spikes"
    ok(ws, "edit", "DEMO-0001", "--type", "feature")
    assert "publish_to" not in card(ws, "DEMO-0001")


def test_reopen_and_reclose_overwrites_the_published_copy(ws):
    ok(ws, "new", "spike", "Try caching", "--publish-to", "docs")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    write_findings(ws, "## Findings\n\nFirst take.\n")
    ok(ws, "close", "DEMO-0001")
    published = ws.root / "docs" / "spike_DEMO-0001_try-caching.md"
    assert "First take." in published.read_text(encoding="utf-8")
    ok(ws, "reopen", "DEMO-0001")
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    write_findings(ws, "## Findings\n\nSecond take.\n")
    ok(ws, "close", "DEMO-0001")
    text = published.read_text(encoding="utf-8")
    assert "Second take." in text and "First take." not in text


def test_missing_summary_is_refused_unless_forced(ws):
    ok(ws, "new", "spike", "Try caching", "--publish-to", "docs")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    git(ws, "rm", "-q", SUMMARY, cwd=ws.worktree("agent-a"))
    git(ws, "commit", "-q", "-m", "DEMO-0001: drop summary", cwd=ws.worktree("agent-a"))
    err = mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert f"DEMO-0001 has no summary {SUMMARY} on {BRANCH} to publish to docs" in err
    assert card(ws, "DEMO-0001")["status"] == "in-progress"
    out = ok(ws, "close", "DEMO-0001", "--force")
    assert f"warning: DEMO-0001 has no summary {SUMMARY}; nothing was published" in out
    assert card(ws, "DEMO-0001")["status"] == "done"
    assert not (ws.root / "docs").exists()
