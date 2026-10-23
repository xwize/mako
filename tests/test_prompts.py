"""Prompt files in .mako/prompts/: missing, edited, braces, and upgrades."""
import json
import re
import shutil

import pytest
from helpers import SRC, mako, mcp_err, mcp_guide, mcp_instructions, ok, set_config, text_sha256_normalized

PROMPTS = ("common", "worker", "orchestrator", "guide_review", "guide_cli", "guide_hooks",
           "guide_review_agents", "guide_review_gerrit", "guide_review_off", "guide_hooks_off", "claude-pointer")


def prompts(ws):
    return ws.root / ".mako" / "prompts"


def test_init_installs_every_prompt(ws):
    for name in PROMPTS:
        assert (prompts(ws) / f"{name}.txt").is_file(), name


GUIDE_PARTS = {  # which prompt files each agent is shown, in order
    ("agent-a", False): ["common", "worker", "guide_review_off", "guide_hooks_off", "guide_cli"],
    ("agent-a", True): ["common", "worker", "guide_review", "guide_hooks_off", "guide_cli"],
    ("orchestrator", False): ["common", "orchestrator", "guide_cli"],
    ("orchestrator", True): ["common", "orchestrator", "guide_cli"],
}


@pytest.mark.parametrize("agent", ["agent-a", "orchestrator"])
@pytest.mark.parametrize("review", [False, True], ids=["no-review", "review"])
def test_each_agent_is_shown_its_prompt_files(ws, agent, review):
    if review:
        set_config(ws, review_hook="difit {base}")
    default = mcp_guide(ws, agent)
    assert not re.search(r"\{(root|project|base|prefix|agent|agents|worktree|review_cli)\}", default)
    assert ("`mako review <ID>`" in default) == review

    for name in ("common", "worker", "orchestrator", "guide_review", "guide_review_off", "guide_hooks_off", "guide_cli"):
        with open(prompts(ws) / f"{name}.txt", "a", encoding="utf-8") as f:
            f.write(f"\nMARK-{name} agent={{agent}} base={{base}}\n")
    # The MCP `guide` tool and `mako guide` give the same text.
    guide = mcp_guide(ws, agent)
    assert ok(ws, "guide", "--agent", agent).rstrip("\n") == guide.rstrip("\n")

    shown = re.findall(r"MARK-(\w+) agent=(\S*) base=main", guide)
    assert shown == [(name, agent) for name in GUIDE_PARTS[agent, review]]
    # The instructions are the core only; a topic is just its own files.
    core = re.findall(r"MARK-(\w+)", mcp_instructions(ws, agent))
    assert core == [n for n in GUIDE_PARTS[agent, review] if not n.startswith("guide_")]
    assert re.findall(r"MARK-(\w+)", mcp_guide(ws, agent, "cli")) == ["guide_cli"]
    assert ok(ws, "guide", "cli", "--agent", agent).rstrip("\n") == mcp_guide(ws, agent, "cli").rstrip("\n")


def test_workers_list_every_topic_and_read_only_those_that_apply(ws):
    text = mcp_instructions(ws, "agent-a")
    assert "(`review`, `hooks`, `cli`)" in text and "Before closing" not in text
    assert "no review step" in mcp_guide(ws, "agent-a", "review")
    assert "no `pre_close_hook`" in mcp_guide(ws, "agent-a", "hooks")
    set_config(ws, auto_approve=False, pre_close_hook="true")
    text = mcp_instructions(ws, "agent-a")
    assert "(`review`, `hooks`, `cli`)" in text
    assert "Before closing, read the guide topic `review`" in text and "`hooks` (the checks" in text
    assert "## Code review" in mcp_guide(ws, "agent-a", "review")
    assert "    true\n" in mcp_guide(ws, "agent-a", "hooks")
    assert "(`cli`)" in mcp_instructions(ws, "orchestrator")
    assert "no guide topic 'review' here; topics: cli" in mcp_err(ws, "orchestrator", "guide", topic="review")


def test_init_renames_old_prompt_files_and_keeps_their_edits(ws):
    folder = prompts(ws)
    (folder / "guide_review.txt").rename(folder / "review.txt")
    (folder / "guide_review_agents.txt").rename(folder / "review-agents.txt")
    (folder / "guide_cli.txt").write_text("My own CLI notes.\n", encoding="utf-8")
    (folder / "guide_cli.txt").rename(folder / "cli.txt")
    record = json.loads((folder / ".installed.json").read_text(encoding="utf-8"))
    record["review.txt"], record["cli.txt"] = record.pop("guide_review.txt"), record.pop("guide_cli.txt")
    (folder / ".installed.json").write_text(json.dumps(record), encoding="utf-8")

    out = ok(ws, "init")
    assert "renamed .mako/prompts/cli.txt to guide_cli.txt" in out
    assert not [p.name for p in folder.glob("*.txt") if not p.name.startswith("guide_")
                and p.stem not in ("common", "worker", "orchestrator", "claude-pointer")]
    assert (folder / "guide_review_agents.txt").is_file()
    assert (folder / "guide_cli.txt").read_text(encoding="utf-8") == "My own CLI notes.\n"
    assert (folder / "guide_review.txt").read_bytes() == (SRC / "templates" / "prompts" / "guide_review.txt").read_bytes()


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
    new = fake_release(tmp_path, {"guide_cli.txt": "\nNEW CLI LINE\n", "worker.txt": "\nNEW WORKER LINE\n"})

    out = mako(ws, "init", script=new).out
    assert "updated .mako/prompts/guide_cli.txt (new default)" in out
    assert ("note: .mako/prompts/worker.txt is edited, so it was kept; the default changed, "
            "see .mako/templates/prompts/worker.txt") in out
    assert "NEW CLI LINE" in (prompts(ws) / "guide_cli.txt").read_text(encoding="utf-8")
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
