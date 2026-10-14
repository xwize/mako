"""`mako session`: whether agent.cmd's resume has a Claude conversation to continue."""
import re
from pathlib import Path

from helpers import mako


def test_session_finds_a_claude_conversation(ws):
    """It looks in the test's own CLAUDE_CONFIG_DIR."""
    assert mako(ws, "session").code == 1
    name = re.sub(r"[^a-zA-Z0-9]", "-", str(ws.root.resolve()))
    folder = Path(ws.env["CLAUDE_CONFIG_DIR"]) / "projects" / name
    folder.mkdir(parents=True)
    (folder / "conversation.jsonl").write_text("{}\n", encoding="utf-8")
    assert mako(ws, "session").code == 0
