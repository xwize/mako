"""Snapshot tests: the guides, a rendered board and the generated launch files.

After a deliberate change, rewrite them with `python scripts/test.py --update-snapshots`
and review the diff of tests/snapshots/ like any other change.
"""
import re
from pathlib import Path

import pytest
from helpers import commit, drag, mako, mcp_instructions, ok, set_config, snapshot, type_card


@pytest.mark.parametrize("agent", ["agent-a", "orchestrator"])
@pytest.mark.parametrize("review", [False, True], ids=["no-review", "review"])
def test_guide(ws, request, agent, review):
    if review:
        set_config(ws, review_command="difit {base}")
    guide = ok(ws, "guide", "--agent", agent)
    # The MCP server's instructions are the same text.
    assert mcp_instructions(ws, agent).rstrip("\n") == guide.rstrip("\n")
    suffix = "-review" if review else ""
    snapshot(request, f"guide-{agent}{suffix}.md", ws.normalize(guide))


def test_board(ws, request):
    ok(ws, "new", "feature", "Done feature")
    ok(ws, "new", "bug", "In progress bug")
    ok(ws, "new", "spike", "Done spike")
    ok(ws, "new", "feature", "Blocked one", "--depends", "DEMO-0002")
    ok(ws, "new", "feature", "Discarded one")
    for cid, slot in (("DEMO-0001", "agent-a"), ("DEMO-0003", "agent-b")):
        ok(ws, "claim", cid, "--agent", slot)
        commit(ws, slot, f"{cid}.txt", "x\n")
        ok(ws, "close", cid)
    ok(ws, "claim", "DEMO-0002", "--agent", "agent-a")
    ok(ws, "discard", "DEMO-0005")
    type_card(ws, "backlog", "bug: Typed by hand")
    drag(ws, "DEMO-0004", "backlog")  # to the top
    ok(ws, "sync")
    snapshot(request, "board.md", (ws.root / "kanban" / "board.md").read_text(encoding="utf-8"))
    snapshot(request, "list.txt", ok(ws, "list", "--all"))


def test_launch_files(ws, request):
    # launch/resume only open Windows Terminal, so only the files they run are checked.
    for name in ("mako.launch.bat", "mako.cmd", ".mako/agent.cmd", ".mako/bin/mako.cmd", ".mako/bin/mako"):
        text = (ws.root / name).read_bytes().decode("utf-8")
        snapshot(request, "launch-" + name.replace("/", "_").lstrip("._") + ".txt", ws.normalize(text))


def test_session_finds_a_claude_conversation(ws):
    """`mako session` (used by agent.cmd for resume) looks in the test's own CLAUDE_CONFIG_DIR."""
    assert mako(ws, "session").code == 1
    name = re.sub(r"[^a-zA-Z0-9]", "-", str(ws.root.resolve()))
    folder = Path(ws.env["CLAUDE_CONFIG_DIR"]) / "projects" / name
    folder.mkdir(parents=True)
    (folder / "conversation.jsonl").write_text("{}\n", encoding="utf-8")
    assert mako(ws, "session").code == 0
