"""Prompt files in .mako/prompts/: missing, edited, braces, and upgrades."""
import re
import shutil

import pytest
from helpers import SRC, mako, mcp_guide, mcp_instructions, ok, set_config, text_sha256_normalized

PROMPTS = ("common", "worker", "orchestrator", "review", "cli", "claude-pointer",
           "brief-worker", "brief-orchestrator")


def prompts(ws):
    return ws.root / ".mako" / "prompts"


def test_init_installs_every_prompt(ws):
    for name in PROMPTS:
        assert (prompts(ws) / f"{name}.txt").is_file(), name


GUIDE_PARTS = {  # which prompt files each agent is shown, in order
    ("agent-a", False): ["common", "worker", "cli"],
    ("agent-a", True): ["common", "worker", "review", "cli"],
    ("orchestrator", False): ["common", "orchestrator", "cli"],
    ("orchestrator", True): ["common", "orchestrator", "cli"],
}


@pytest.mark.parametrize("agent", ["agent-a", "orchestrator"])
@pytest.mark.parametrize("review", [False, True], ids=["no-review", "review"])
def test_each_agent_is_shown_its_prompt_files(ws, agent, review):
    if review:
        set_config(ws, review_hook="difit {base}")
    default = mcp_guide(ws, agent)
    assert not re.search(r"\{(root|project|base|prefix|agent|agents|worktree|review_cli)\}", default)
    assert ("`mako review <ID>`" in default) == review

    for name in ("common", "worker", "orchestrator", "review", "cli"):
        with open(prompts(ws) / f"{name}.txt", "a", encoding="utf-8") as f:
            f.write(f"\nMARK-{name} agent={{agent}} base={{base}}\n")
    # The MCP `guide` tool and `mako guide` give the same text.
    guide = mcp_guide(ws, agent)
    assert ok(ws, "guide", "--agent", agent).rstrip("\n") == guide.rstrip("\n")

    shown = re.findall(r"MARK-(\w+) agent=(\S*) base=main", guide)
    assert shown == [(name, agent) for name in GUIDE_PARTS[agent, review]]
    assert "MARK-" not in mcp_instructions(ws, agent)


@pytest.mark.parametrize("agent", ["agent-a", "orchestrator"])
def test_instructions_are_the_brief_for_the_role(ws, agent):
    brief = "brief-worker" if agent == "agent-a" else "brief-orchestrator"
    other = "brief-orchestrator" if agent == "agent-a" else "brief-worker"
    for name in (brief, other):
        with open(prompts(ws) / f"{name}.txt", "a", encoding="utf-8") as f:
            f.write(f"\nMARK-{name} agent={{agent}} base={{base}}\n")
    text = mcp_instructions(ws, agent)
    assert re.findall(r"MARK-([\w-]+) agent=(\S*) base=main", text) == [(brief, agent)]
    assert "MARK-" not in mcp_guide(ws, agent)


def test_missing_file_shows_the_message_and_init_restores_it(ws):
    (prompts(ws) / "worker.txt").unlink()
    guide = ok(ws, "guide", "--agent", "agent-a")
    assert "**`.mako/prompts/worker.txt` is missing" in guide and "run `mako init`" in guide
    assert "## You are worker slot" not in guide
    assert "## CLI" in guide  # the other parts are still there
    assert "created .mako/prompts/worker.txt" in ok(ws, "init")
    assert "## You are worker slot `agent-a`" in ok(ws, "guide", "--agent", "agent-a")


def test_edit_changes_only_its_part_and_braces_pass_through(ws):
    before = {a: ok(ws, "guide", "--agent", a) for a in ("agent-a", "orchestrator")}
    with open(prompts(ws) / "orchestrator.txt", "a", encoding="utf-8") as f:
        f.write('\nEDITED {"a": 1} {unknown} {{x}} slots={agents} base={base}\n')
    assert ok(ws, "guide", "--agent", "agent-a") == before["agent-a"]
    after = ok(ws, "guide", "--agent", "orchestrator")
    assert 'EDITED {"a": 1} {unknown} {{x}} slots=agent-a, agent-b base=main' in after
    assert after.replace('\n\nEDITED {"a": 1} {unknown} {{x}} slots=agent-a, agent-b base=main', "") == \
        before["orchestrator"]


def fake_release(tmp_path, changes: dict) -> object:
    """A copy of src/ whose shipped prompt defaults have changed."""
    rel = tmp_path / "release"
    shutil.copytree(SRC, rel)
    for name, extra in changes.items():
        with open(rel / "templates" / "prompts" / name, "a", encoding="utf-8") as f:
            f.write(extra)
    return rel / "mako.py"


def test_upgrade_updates_unedited_and_keeps_edited(ws, tmp_path):
    with open(prompts(ws) / "worker.txt", "a", encoding="utf-8") as f:
        f.write("\nMy own step.\n")
    new = fake_release(tmp_path, {"cli.txt": "\nNEW CLI LINE\n", "worker.txt": "\nNEW WORKER LINE\n"})

    out = mako(ws, "init", script=new).out
    assert "updated .mako/prompts/cli.txt (new default)" in out
    assert ("note: .mako/prompts/worker.txt is edited, so it was kept; the default changed, "
            "see .mako/templates/prompts/worker.txt") in out
    assert "NEW CLI LINE" in (prompts(ws) / "cli.txt").read_text(encoding="utf-8")
    worker = (prompts(ws) / "worker.txt").read_text(encoding="utf-8")
    assert "My own step." in worker and "NEW WORKER LINE" not in worker
    # The note is said once per new default.
    assert "note:" not in mako(ws, "init", script=new).out


def test_line_endings_alone_are_not_an_edit(ws, tmp_path):
    path = prompts(ws) / "common.txt"
    # LF first: in a core.autocrlf=true checkout the installed file is already CRLF.
    lf_bytes = path.read_bytes().replace(b"\r\n", b"\n")
    path.write_bytes(lf_bytes.replace(b"\n", b"\r\n"))
    default = (SRC / "templates" / "prompts" / "common.txt").read_bytes()
    assert text_sha256_normalized(path.read_bytes()) == text_sha256_normalized(default)
    new = fake_release(tmp_path, {"common.txt": "\nNEW COMMON LINE\n"})
    assert "updated .mako/prompts/common.txt (new default)" in mako(ws, "init", script=new).out


def test_claude_md_pointer_comes_from_its_prompt_file(ws):
    first_line = (ws.root / "CLAUDE.md").read_text(encoding="utf-8").splitlines()[0]
    assert first_line == (prompts(ws) / "claude-pointer.txt").read_text(encoding="utf-8").strip()
    # An edited pointer is used for a new CLAUDE.md, placeholders and braces included.
    (prompts(ws) / "claude-pointer.txt").write_text("Pointer for {project} {x}\n", encoding="utf-8")
    (ws.root / "CLAUDE.md").unlink()
    ok(ws, "init")
    assert (ws.root / "CLAUDE.md").read_text(encoding="utf-8") == "Pointer for demo {x}\n\n# demo\n"
    # An existing CLAUDE.md is never touched.
    (ws.root / "CLAUDE.md").write_text("mine\n", encoding="utf-8")
    ok(ws, "init")
    assert (ws.root / "CLAUDE.md").read_text(encoding="utf-8") == "mine\n"
