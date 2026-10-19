"""Hooks: new_hook, claim_hook, review_hook and close_hook run the user's commands, detached, with
the issue's parameters as {name} placeholders and MAKO_<NAME> environment variables."""
import json
import sys

from helpers import card, commit, fails, git, mcp_err, mcp_ok, ok, set_config, type_card, wait_for
from test_review_backend import use_stub

RECORDER = """\
import json, os, sys
env = {k: v for k, v in os.environ.items() if k.upper().startswith("MAKO_")}
with open(sys.argv[1], "w", encoding="utf-8") as out:
    out.write(json.dumps({"argv": sys.argv[2:], "cwd": os.getcwd(), "env": env}))
"""


def recorder(tmp_path, name: str, args: str = "") -> tuple[str, object]:
    """A hook command that records its arguments, folder and MAKO_* environment in <name>.json."""
    script = tmp_path / "record.py"
    script.write_text(RECORDER, encoding="utf-8")
    out = tmp_path / f"{name}.json"
    return f'"{sys.executable}" "{script}" "{out}" {args}'.rstrip(), out


def recorded(out) -> dict:
    return json.loads(wait_for(out))


def same(a, b) -> bool:
    return str(a).replace("\\", "/").lower().rstrip("/") == str(b).replace("\\", "/").lower().rstrip("/")


def log(ws, cid: str, event: str):
    return ws.root / ".mako" / "logs" / f"{cid}.{event}.log"


def test_new_hook_runs_on_every_kind_of_creation(ws, tmp_path):
    cmd, out = recorder(tmp_path, "new", '{id} "{title}" {type} "{root}" "[{branch}]" "[{worktree}]"')
    set_config(ws, new_hook=cmd)
    ok(ws, "new", "bug", "Via CLI")
    seen = recorded(out)
    assert seen["argv"][:3] == ["DEMO-0001", "Via CLI", "bug"]
    assert same(seen["argv"][3], ws.root) and seen["argv"][4:] == ["[]", "[]"]
    assert same(seen["cwd"], ws.root)
    env = seen["env"]
    assert env["MAKO_EVENT"] == "new" and env["MAKO_ID"] == "DEMO-0001" and env["MAKO_BRANCH"] == ""
    assert same(env["MAKO_WORKSPACE"], ws.root) and same(env["MAKO_REPO"], ws.repo)
    assert "MAKO_ROOT" not in env
    assert "$ " in log(ws, "DEMO-0001", "new").read_text(encoding="utf-8")

    out.unlink()
    mcp_ok(ws, "orchestrator", "create_issue", type="feature", title="Via MCP")
    assert recorded(out)["argv"][:2] == ["DEMO-0002", "Via MCP"]

    out.unlink()
    type_card(ws, "backlog", "spike: Typed on the board")
    ok(ws, "list")
    assert recorded(out)["argv"][:3] == ["DEMO-0003", "Typed on the board", "spike"]


def test_claim_hook_gets_the_branch_and_worktree(ws, tmp_path):
    cmd, out = recorder(tmp_path, "claim", '{agent} {branch} {sha} "{worktree}"')
    set_config(ws, claim_hook=cmd)
    ok(ws, "new", "feature", "One")
    ok(ws, "new", "feature", "Two")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    seen = recorded(out)
    sha = git(ws, "rev-parse", "main")
    assert seen["argv"][:3] == ["agent-a", "feature/DEMO-0001-one", sha]
    assert same(seen["argv"][3], ws.worktree("agent-a")) and same(seen["cwd"], ws.worktree("agent-a"))
    assert seen["env"]["MAKO_WORKTREE"] == seen["argv"][3] and seen["env"]["MAKO_EVENT"] == "claim"

    out.unlink()
    ok(ws, "claim", "next", "--agent", "agent-b")
    assert recorded(out)["argv"][:2] == ["agent-b", "feature/DEMO-0002-two"]


def test_close_hook_runs_only_after_a_successful_close(ws, tmp_path):
    cmd, out = recorder(tmp_path, "close", "{id} {branch} {sha} {agent}")
    set_config(ws, close_hook=cmd)
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    sha = commit(ws, "agent-a", "a.txt", "a\n")
    (ws.worktree("agent-a") / "dirty.txt").write_text("x", encoding="utf-8")
    assert "uncommitted changes" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert not log(ws, "DEMO-0001", "close").exists()

    (ws.worktree("agent-a") / "dirty.txt").unlink()
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    seen = recorded(out)
    assert seen["argv"] == ["DEMO-0001", "feature/DEMO-0001-one", sha, "agent-a"]
    assert same(seen["cwd"], ws.repo)


def test_a_list_launches_every_command(ws, tmp_path):
    first, out1 = recorder(tmp_path, "one", "{id}")
    second, out2 = recorder(tmp_path, "two", "{id}")
    set_config(ws, new_hook=[first, "", second])
    ok(ws, "new", "feature", "One")
    assert recorded(out1)["argv"] == recorded(out2)["argv"] == ["DEMO-0001"]
    headers = log(ws, "DEMO-0001", "new").read_text(encoding="utf-8").count("# cwd: ")
    assert headers == len([first, second])


def test_braces_and_unknown_placeholders(ws, tmp_path):
    cmd, out = recorder(tmp_path, "new", '"{{id}}" "{nope}" "{id}"')
    set_config(ws, new_hook=cmd)
    text = ok(ws, "new", "feature", "One")
    assert "unknown parameter {nope} left as it is" in text
    assert recorded(out)["argv"] == ["{id}", "{nope}", "DEMO-0001"]


def test_hook_params(ws, tmp_path):
    cmd, out = recorder(tmp_path, "claim", '"{where}" "{tool}"')
    set_config(ws, claim_hook=cmd, hook_params={"where": "{worktree}/src", "tool": "code", "id": "x"})
    ok(ws, "new", "feature", "One")
    text = mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    assert "can't override the built-in parameter {id}" in text
    seen = recorded(out)
    assert same(seen["argv"][0], f"{ws.worktree('agent-a')}/src") and seen["argv"][1] == "code"
    assert seen["env"]["MAKO_PARAM_TOOL"] == "code" and seen["env"]["MAKO_ID"] == "DEMO-0001"
    assert "MAKO_PARAM_ID" not in seen["env"]


def test_review_hook_is_the_review_step_and_wins_over_review_command(ws, tmp_path):
    cmd, out = recorder(tmp_path, "review", "{id} {sha}")
    set_config(ws, review_hook=cmd, review_command="never-run")
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    sha = commit(ws, "agent-a", "a.txt", "a\n")
    assert "call `request_review` first" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    text = mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert "review_hook is used" in text
    assert recorded(out)["argv"] == ["DEMO-0001", sha]
    assert "never-run" not in log(ws, "DEMO-0001", "review").read_text(encoding="utf-8")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")


def test_review_hook_runs_after_the_backend(ws, tmp_path):
    cmd, out = recorder(tmp_path, "review", "{review_ref} [{review_url}]")
    use_stub(ws)
    set_config(ws, review_hook=cmd)
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "stub review of DEMO-0001" in mcp_ok(ws, "agent-a", "request_review", id="DEMO-0001")
    assert recorded(out)["argv"] == ["7", "[]"]  # the stub returns no url


def test_review_hook_does_not_run_when_the_backend_fails(ws, tmp_path):
    cmd, _ = recorder(tmp_path, "review", "{id}")
    use_stub(ws, down=True)
    set_config(ws, review_hook=cmd)
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "stub is down" in mcp_err(ws, "agent-a", "request_review", id="DEMO-0001")
    assert not log(ws, "DEMO-0001", "review").exists()


def test_a_launch_failure_only_warns_except_for_the_review_step(ws, tmp_path):
    cmd, _ = recorder(tmp_path, "any", "{id}")
    set_config(ws, new_hook=cmd, review_hook=cmd)
    for event in ("new", "review"):  # a folder where the log goes: the hook can't start
        log(ws, "DEMO-0001", event).mkdir(parents=True)
    assert "warning: could not launch the new_hook for DEMO-0001" in ok(ws, "new", "feature", "One")
    assert card(ws, "DEMO-0001")["status"] == "backlog"
    ok(ws, "claim", "DEMO-0001", "--agent", "agent-a")
    commit(ws, "agent-a", "a.txt", "a\n")
    assert "could not launch the review_hook" in fails(ws, "review", "DEMO-0001")
    assert card(ws, "DEMO-0001")["reviews"] == "0"


def test_init_lists_the_hooks_and_moves_review_command(ws):
    path = ws.root / ".mako" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert [config[k] for k in ("new_hook", "claim_hook", "review_hook", "close_hook")] == ["", "", "", ""]
    assert config["hook_params"] == {} and "review_command" not in config
    set_config(ws, claim_hook=["a", "b"], hook_params={"ide": "code"}, review_command="difit {base}")
    ok(ws, "init")
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["claim_hook"] == ["a", "b"] and config["hook_params"] == {"ide": "code"}
    assert config["review_hook"] == "difit {base}" and "review_command" not in config
    set_config(ws, review_command="old")
    ok(ws, "init")  # review_hook is set: the legacy value stays where it is
    config = json.loads(path.read_text(encoding="utf-8"))
    assert (config["review_hook"], config["review_command"]) == ("difit {base}", "old")
