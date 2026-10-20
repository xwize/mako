"""pre_close_hook: the user's checks, run and waited for before close merges. A failure refuses
the close and changes nothing; only a human's `mako close --force` skips them."""
import json
import sys

from helpers import card, commit, fails, git, mcp_err, mcp_instructions, mcp_ok, ok, set_config

CHECK = """\
import json, os, sys
for i in range(30):
    print(f"line {i}")
with open(sys.argv[1], "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": sys.argv[2:], "cwd": os.getcwd(),
                          "worktree": os.environ.get("MAKO_WORKTREE"), "sha": os.environ.get("MAKO_SHA")}) + "\\n")
if not os.path.exists("ok.txt"):
    print("ok.txt is missing")
    sys.exit(3)
"""


def checker(tmp_path, args: str = "") -> tuple[str, object]:
    """A check that passes only when ok.txt is in its folder; each run is appended to runs.jsonl."""
    script = tmp_path / "check.py"
    script.write_text(CHECK, encoding="utf-8")
    runs = tmp_path / "runs.jsonl"
    return f'"{sys.executable}" "{script}" "{runs}" {args}'.rstrip(), runs


def runs(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def same(a, b) -> bool:
    return str(a).replace("\\", "/").lower().rstrip("/") == str(b).replace("\\", "/").lower().rstrip("/")


def claimed_with_work(ws, slot: str = "agent-a") -> str:
    ok(ws, "new", "feature", "One")
    mcp_ok(ws, slot, "claim_issue", id="DEMO-0001")
    return commit(ws, slot, "a.txt", "a\n")


def test_a_failing_check_refuses_the_close_and_changes_nothing(ws, tmp_path):
    check, log = checker(tmp_path, "{id}")
    set_config(ws, pre_close_hook=[check, f'"{sys.executable}" -c "print(1)" never'])
    claimed_with_work(ws)
    base = git(ws, "rev-parse", "main")
    text = mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert "failed with exit code 3" in text and "DEMO-0001 was not closed" in text
    assert "DEMO-0001.pre_close.log" in text and "ok.txt is missing" in text and "line 29" in text
    assert "line 5" not in text  # only the tail
    assert len(runs(log)) == 1  # stopped at the first failure
    assert "never" not in (ws.root / ".mako" / "logs" / "DEMO-0001.pre_close.log").read_text(encoding="utf-8")
    assert card(ws, "DEMO-0001")["status"] == "in-progress"
    assert (ws.root / ".mako" / "locks" / "DEMO-0001").is_dir()
    assert git(ws, "rev-parse", "main") == base

    # fixed on the branch: the close goes through
    sha = commit(ws, "agent-a", "ok.txt", "ok\n")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    seen = runs(log)[-1]
    assert seen["argv"] == ["DEMO-0001"] and seen["sha"] == sha
    assert same(seen["cwd"], ws.worktree("agent-a")) and same(seen["worktree"], ws.worktree("agent-a"))
    assert card(ws, "DEMO-0001")["status"] == "done"
    assert git(ws, "merge-base", "--is-ancestor", sha, "main") == ""


def test_skipped_for_spikes_and_when_nothing_to_merge(ws, tmp_path):
    check, log = checker(tmp_path)
    set_config(ws, pre_close_hook=check)
    ok(ws, "new", "feature", "Nothing")
    ok(ws, "new", "spike", "Look")
    mcp_ok(ws, "agent-a", "claim_issue", id="DEMO-0001")
    mcp_ok(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    mcp_ok(ws, "agent-b", "claim_issue", id="DEMO-0002")
    commit(ws, "agent-b", "notes.txt", "n\n")
    mcp_ok(ws, "agent-b", "close_issue", id="DEMO-0002", note="x")
    assert runs(log) == []


def test_only_a_humans_force_skips_it(ws, tmp_path):
    check, log = checker(tmp_path)
    set_config(ws, pre_close_hook=check)
    claimed_with_work(ws)
    assert "pre_close_hook" in fails(ws, "close", "DEMO-0001", "--force", "--agent", "agent-a")
    assert "pre_close_hook" in fails(ws, "close", "DEMO-0001")
    tried = len(runs(log))
    out = ok(ws, "close", "DEMO-0001", "--force")
    assert "warning: --force: the pre_close_hook was not run for DEMO-0001" in out
    assert card(ws, "DEMO-0001")["status"] == "done" and len(runs(log)) == tried


def test_an_unclaimed_branch_is_checked_in_a_temporary_worktree(ws, tmp_path):
    check, log = checker(tmp_path)
    set_config(ws, pre_close_hook=check)
    claimed_with_work(ws)
    commit(ws, "agent-a", "ok.txt", "ok\n")
    mcp_ok(ws, "agent-a", "release_issue", id="DEMO-0001", note="x")
    ok(ws, "close", "DEMO-0001")
    temp = ws.root / ".mako" / "tmp" / "pre_close-DEMO-0001"
    assert same(runs(log)[0]["cwd"], temp) and not temp.exists()
    assert "pre_close-DEMO-0001" not in git(ws, "worktree", "list")
    assert card(ws, "DEMO-0001")["status"] == "done"


def test_a_branch_that_moves_during_the_check_is_refused(ws, tmp_path):
    set_config(ws, pre_close_hook='git commit -q --allow-empty -m "sneaked in"')
    claimed_with_work(ws)
    assert "changed while the pre_close_hook ran" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert card(ws, "DEMO-0001")["status"] == "in-progress"


def test_checks_that_leave_the_worktree_unclean_refuse(ws, tmp_path):
    set_config(ws, pre_close_hook=f'"{sys.executable}" -c "open(\'build.out\', \'w\').close()"')
    claimed_with_work(ws)
    assert "left changes in" in mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert card(ws, "DEMO-0001")["status"] == "in-progress"


def test_timeout_kills_the_check_and_refuses(ws, tmp_path):
    set_config(ws, pre_close_hook=f'"{sys.executable}" -c "import time; time.sleep(60)"', pre_close_timeout=1)
    claimed_with_work(ws)
    text = mcp_err(ws, "agent-a", "close_issue", id="DEMO-0001", note="x")
    assert "timed out after 1s (killed)" in text
    assert card(ws, "DEMO-0001")["status"] == "in-progress"


def test_init_lists_it_and_the_guide_mentions_it_only_when_set(ws):
    config = json.loads((ws.root / ".mako" / "config.json").read_text(encoding="utf-8"))
    assert config["pre_close_hook"] == "" and config["pre_close_timeout"] == 0
    assert "Checks before close" not in mcp_instructions(ws, "agent-a")
    set_config(ws, pre_close_hook=["make all", "make test"])
    text = mcp_instructions(ws, "agent-a")
    assert "Checks before close" in text and "    make all\n    make test\n" in text
    assert "Checks before close" not in mcp_instructions(ws, "orchestrator")
