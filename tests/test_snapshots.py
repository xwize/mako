"""Snapshot tests: a rendered board, `mako list` and the generated launch files.

After a deliberate change, rewrite them with `python scripts/test.py --update-snapshots`
and review the diff of tests/snapshots/ like any other change.
"""
import re
from pathlib import Path

from helpers import commit, drag, mako, ok, snapshot, type_card


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
