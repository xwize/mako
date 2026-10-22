"""Project-scoped Claude session names: <project>-<slot>-<tag>, passed to `claude -n` by agent.cmd."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import fails, mako, ok, run

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mako_core  # noqa: E402
import mako_init  # noqa: E402


def read_config(ws) -> dict:
    return json.loads((ws.root / ".mako" / "config.json").read_text(encoding="utf-8"))


def write_config(ws, config: dict) -> None:
    (ws.root / ".mako" / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def test_init_writes_a_four_hex_tag_and_names_use_it(ws):
    tag = read_config(ws)["session_tag"]
    assert re.fullmatch(r"[0-9a-f]{4}", tag)
    assert ok(ws, "session-name", "agent-a").strip() == f"demo-agent-a-{tag}"
    assert ok(ws, "session-name", "orchestrator").strip() == f"demo-orchestrator-{tag}"


def test_rerunning_init_keeps_the_tag(ws):
    before = read_config(ws)["session_tag"]
    ok(ws, "init")
    ok(ws, "init")
    assert read_config(ws)["session_tag"] == before


def test_init_backfills_a_missing_tag_once(ws):
    config = read_config(ws)
    del config["session_tag"]
    write_config(ws, config)
    assert "no session_tag" in fails(ws, "session-name", "agent-a")
    ok(ws, "init")
    tag = read_config(ws)["session_tag"]
    assert re.fullmatch(r"[0-9a-f]{4}", tag)
    ok(ws, "init")
    assert read_config(ws)["session_tag"] == tag


def test_an_existing_tag_is_never_changed(ws):
    config = read_config(ws)
    config["session_tag"] = "abcd"
    write_config(ws, config)
    ok(ws, "init")
    assert read_config(ws)["session_tag"] == "abcd"
    assert ok(ws, "session-name", "agent-b").strip() == "demo-agent-b-abcd"


def test_unknown_slot_is_refused(ws):
    assert "unknown slot 'nope'" in fails(ws, "session-name", "nope")


def test_project_is_lowercased_and_sanitised():
    config = {"project": "My Proj.v2 [x]", "session_tag": "3f9c"}
    assert mako_core.render_session_name(config, "agent-a") == "my-proj-v2--x--agent-a-3f9c"
    config = {"project": "Mako_Dev", "session_tag": "3f9c"}
    assert mako_core.render_session_name(config, "orchestrator") == "mako_dev-orchestrator-3f9c"


def test_tags_differ_between_workspaces_and_runs(tmp_path):
    tags = {mako_core.new_session_tag(tmp_path) for _ in range(20)}
    assert len(tags) > 1
    assert all(re.fullmatch(r"[0-9a-f]{4}", t) for t in tags)


def test_name_survives_moving_the_workspace(ws, tmp_path):
    before = ok(ws, "session-name", "agent-a")
    moved = tmp_path / "elsewhere"
    shutil.copytree(ws.root / ".mako", moved / ".mako", ignore=shutil.ignore_patterns("worktrees"))
    r = run([sys.executable, str(moved / ".mako" / "mako.py"), "session-name", "agent-a"],
            moved, {**ws.env, "MAKO_ROOT": str(moved)})
    assert r.code == 0, r.text
    assert r.out == before


# --- the generated agent.cmd, run against a fake `claude` ----------------------------

@pytest.mark.skipif(os.name != "nt", reason="agent.cmd is a Windows batch file")
class TestAgentCmd:
    @staticmethod
    def launch(ws, tmp_path, *args) -> str:
        """Run agent.cmd with a fake claude that records its arguments; returns them."""
        fake = tmp_path / "fakebin"
        fake.mkdir(exist_ok=True)
        log = tmp_path / "claude-args.txt"
        log.unlink(missing_ok=True)
        (fake / "claude.cmd").write_text(f'@echo %*> "{log}"\n', encoding="utf-8")
        env = {**ws.env, "PATH": f"{fake}{os.pathsep}{ws.env.get('PATH', os.environ['PATH'])}"}
        r = subprocess.run(["cmd", "/c", str(ws.root / ".mako" / "agent.cmd"), *args], cwd=ws.root, env=env,
                           capture_output=True, text=True, timeout=60, check=False)
        assert r.returncode == 0, r.stdout + r.stderr
        return log.read_text(encoding="utf-8")

    @staticmethod
    def add_conversation(ws, slot) -> None:
        cwd = ws.root / ".mako" / "worktrees" / slot
        folder = Path(ws.env["CLAUDE_CONFIG_DIR"]) / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
        folder.mkdir(parents=True)
        (folder / "conversation.jsonl").write_text("{}\n", encoding="utf-8")

    def test_fresh_launch_is_named(self, ws, tmp_path):
        tag = read_config(ws)["session_tag"]
        args = self.launch(ws, tmp_path, "agent-a")
        assert f"-n demo-agent-a-{tag} " in args
        assert "-c " not in args

    def test_orchestrator_is_named(self, ws, tmp_path):
        tag = read_config(ws)["session_tag"]
        assert f"-n demo-orchestrator-{tag} " in self.launch(ws, tmp_path, "orchestrator")

    def test_resume_without_a_conversation_is_named(self, ws, tmp_path):
        tag = read_config(ws)["session_tag"]
        args = self.launch(ws, tmp_path, "agent-b", "resume")
        assert f"-n demo-agent-b-{tag} " in args
        assert "-c " not in args

    def test_resume_continues_and_keeps_the_name(self, ws, tmp_path):
        tag = read_config(ws)["session_tag"]
        self.add_conversation(ws, "agent-a")
        args = self.launch(ws, tmp_path, "agent-a", "resume")
        assert args.startswith("-c ")
        assert f"-n demo-agent-a-{tag} " in args

    def test_no_tag_means_no_name_but_still_launches(self, ws, tmp_path):
        config = read_config(ws)
        del config["session_tag"]
        write_config(ws, config)
        args = self.launch(ws, tmp_path, "agent-a")
        assert "-n " not in args
        assert "--mcp-config" in args


def test_generated_agent_cmd_passes_the_name(ws):
    agent = mako_init.AGENT_CMD.format(project="demo", title=mako_init.safe_title("demo"))
    assert "session-name %SLOT%" in agent
    assert mako(ws, "session-name", "agent-a").code == 0
