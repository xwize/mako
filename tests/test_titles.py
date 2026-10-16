"""Pane titles in the generated launcher and agent.cmd: "<project> [<slot>]"."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mako as mako_mod  # noqa: E402


def test_launcher_titles_include_project_and_slot(ws):
    bat = (ws.root / "mako.launch.bat").read_text(encoding="utf-8")
    for slot in ("orchestrator", "agent-a", "agent-b"):
        assert f'--title "demo [{slot}]" --suppressApplicationTitle' in bat
    agent = (ws.root / ".mako" / "agent.cmd").read_text(encoding="utf-8")
    assert "title demo [%SLOT%]" in agent


def test_special_characters_are_sanitised():
    assert mako_mod.pane_title('a & b^%c";d', "agent-a") == "a _ b__c__d [agent-a]"
