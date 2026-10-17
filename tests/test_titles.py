"""Pane titles in the generated launcher and agent.cmd: "<project> [<slot>]"."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mako as mako_mod  # noqa: E402


# Rendered here, not read from the workspace: init writes the Windows files only on Windows.
def test_launcher_titles_include_project_and_slot():
    bat = mako_mod.render_launch_bat("demo", ["agent-a", "agent-b"])
    for slot in ("orchestrator", "agent-a", "agent-b"):
        assert f'--title "demo [{slot}]" --suppressApplicationTitle' in bat
    agent = mako_mod.AGENT_CMD.format(project="demo", title=mako_mod.safe_title("demo"))
    assert "title demo [%SLOT%]" in agent


def test_special_characters_are_sanitised():
    assert mako_mod.pane_title('a & b^%c";d', "agent-a") == "a _ b__c__d [agent-a]"
