"""The MCP initialize instructions: the core for each role, short enough that Claude Code doesn't
truncate them, naming the `guide` topics that hold the rest."""
import json
import sys

import pytest
from helpers import MAKO, mcp_guide, mcp_instructions, new_workspace, run, set_config
from test_gerrit import dead_url

sys.path.insert(0, str(MAKO.parent))
from mako_mcp import MCP_INSTRUCTIONS_LIMIT  # noqa: E402

MARGIN = 296  # stay this far under the limit, so a small edit doesn't tip it over
SLOTS = [f"agent-with-a-rather-long-slot-name-{i}" for i in range(8)]


@pytest.fixture
def worst(tmp_path, base_env):
    """Every optional section on, and long placeholder values: a deep workspace path, many slots."""
    deep = tmp_path.joinpath(*[f"a-deeply-nested-folder-{i}" for i in range(8)], "ws")
    deep.parent.mkdir(parents=True)
    ws = new_workspace(deep, base_env)
    set_config(ws, agents=SLOTS, pre_close_hook=["python -m pytest -q", "python scripts/lint.py --strict"],
               review={"backend": "gerrit", "url": dead_url()}, auto_approve=False)
    return ws


@pytest.mark.parametrize("agent", [SLOTS[-1], "orchestrator"])
def test_instructions_stay_under_the_limit(worst, agent):
    text = mcp_instructions(worst, agent)
    assert len(text) <= MCP_INSTRUCTIONS_LIMIT - MARGIN, len(text)
    assert "(`review`, `hooks`, `cli`)" in text and "Mako owns kanban/" in text


def test_guide_still_has_every_section(worst):
    guide = mcp_guide(worst, SLOTS[0])
    for part in ("## You are worker slot", "## Checks before close", "python scripts/lint.py --strict",
                 "## Code review", "### Reviews happen in Gerrit", "### Reviewing each other's issues", "## CLI"):
        assert part in guide, part
    assert len(guide) > MCP_INSTRUCTIONS_LIMIT  # why the instructions can't simply be the guide
    assert "## CLI" in mcp_guide(worst, "orchestrator")


def test_edited_prompts_over_the_limit_warn(ws):
    with open(ws.root / ".mako" / "prompts" / "worker.txt", "a", encoding="utf-8") as f:
        f.write("\n" + "My own long rule. " * 300 + "\n")
    init = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    r = run([sys.executable, str(MAKO), "mcp"], ws.root, {**ws.env, "MAKO_AGENT": "agent-a"}, stdin=init + "\n")
    assert r.code == 0, r.text
    assert ".mako/prompts/common.txt and .mako/prompts/worker.txt give" in r.err and "Shorten" in r.err
    text = json.loads(r.out.splitlines()[0])["result"]["instructions"]
    # The warning comes first, so Claude Code's truncation can't cut it; nothing is cut by Mako.
    assert text.startswith("**note: .mako/prompts/common.txt") and text.rstrip().endswith("My own long rule.")
    assert len(mcp_instructions(ws, "orchestrator")) <= MCP_INSTRUCTIONS_LIMIT
