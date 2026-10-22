"""agent.cmd builds the MCP config at launch; init writes no agents/*.mcp.json."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mako_init as mako_mod  # noqa: E402


def test_init_writes_no_mcp_config_files(ws):
    assert not list((ws.root / ".mako").glob("agents/*.mcp.json"))


def test_agent_cmd_passes_the_mcp_config_inline_from_its_own_location():
    text = mako_mod.AGENT_CMD.format(project="demo", title=mako_mod.safe_title("demo"))
    assert 'set "MAKOPY=%~dp0mako.py"' in text
    assert '"command\\":\\"python' in text and 'MAKO_AGENT\\":\\"%SLOT%' in text
    assert '--mcp-config "!MCP!"' in text
    assert "setlocal enabledelayedexpansion" in text
    assert ".mcp.json" not in text
