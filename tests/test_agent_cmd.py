"""agent.cmd builds the MCP config at launch; init writes no agents/*.mcp.json."""
from helpers import ok


def test_init_writes_no_mcp_config_files(ws):
    assert not list((ws.root / ".mako").glob("agents/*.mcp.json"))


def test_agent_cmd_passes_the_mcp_config_inline_from_its_own_location(ws):
    text = (ws.root / ".mako" / "agent.cmd").read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert 'set "MAKOPY=%~dp0mako.py"' in text
    assert '"command\\":\\"python' in text and 'MAKO_AGENT\\":\\"%SLOT%' in text
    assert '--mcp-config "!MCP!"' in text
    assert "setlocal enabledelayedexpansion" in text
    assert ".mcp.json" not in text
    ok(ws, "init")
