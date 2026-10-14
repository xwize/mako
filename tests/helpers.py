"""Black-box helpers for Mako's regression tests (fixtures are in conftest.py).

Everything goes through the real CLI (`python src/mako.py ...`) and MCP server
(`python src/mako.py mcp`) as subprocesses, against a fresh workspace made by
`mako init` in pytest's tmp_path. Nothing here touches the real board.
Run them with `python scripts/test.py`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
MAKO = SRC / "mako.py"
SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
NOW = "2026-01-01 00:00"
GIT_DATE = "2026-01-01T00:00:00+00:00"
PROJECT, PREFIX = "demo", "DEMO"
COLUMN_NAMES = {"backlog": "Backlog", "in-progress": "In Progress", "done": "Done"}
GITCONFIG = """\
[user]
\tname = Mako Test
\temail = test@mako.invalid
[core]
\tautocrlf = false
[init]
\tdefaultBranch = main
[commit]
\tgpgsign = false
"""


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


@dataclass
class Workspace:
    root: Path
    env: dict

    @property
    def repo(self) -> Path:
        return self.root / PROJECT

    def worktree(self, slot: str) -> Path:
        return self.root / ".mako" / "worktrees" / slot

    def normalize(self, text: str) -> str:
        """Replace this workspace's absolute path so output can be compared across runs."""
        for form in (json.dumps(str(self.root))[1:-1], str(self.root), self.root.as_posix()):
            text = text.replace(form, "<WS>")
        return text.replace("\r\n", "\n")


# --- environment -----------------------------------------------------------------

def isolated_env(home: Path) -> dict:
    """A fixed environment: the user's git config, Mako identity and Claude
    config can't leak in, and the clock is frozen. home holds the fake config."""
    (home / "gitconfig").write_text(GITCONFIG, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MAKO_", "GIT_")) and k != "CLAUDE_CONFIG_DIR"}
    env.update(
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_DATE=GIT_DATE, GIT_COMMITTER_DATE=GIT_DATE, GIT_TERMINAL_PROMPT="0",
        MAKO_NOW=NOW, CLAUDE_CONFIG_DIR=str(home / "claude"),
        PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def new_workspace(path: Path, base_env: dict, *init_args: str, script: Path | None = None) -> Workspace:
    """`mako init demo` in a new, empty folder (slots agent-a and agent-b)."""
    w = Workspace(path, {**base_env, "MAKO_ROOT": str(path)})
    w.root.mkdir()
    r = mako(w, "init", PROJECT, *init_args, script=script or MAKO)
    assert r.code == 0, r.text
    return w


# --- driving Mako ----------------------------------------------------------------

def run(args: list, cwd: Path, env: dict, stdin: str | None = None) -> Result:
    p = subprocess.run(args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, check=False)
    return Result(p.returncode, p.stdout, p.stderr)


def mako(ws: Workspace, *args: str, script: Path = MAKO) -> Result:
    """The CLI, as the human runs it (no agent identity)."""
    return run([sys.executable, str(script), *args], ws.root, ws.env)


def ok(ws: Workspace, *args: str) -> str:
    """mako(...) that must succeed; returns stdout."""
    r = mako(ws, *args)
    assert r.code == 0, f"mako {' '.join(args)} failed:\n{r.text}"
    return r.out


def fails(ws: Workspace, *args: str) -> str:
    """mako(...) that must fail; returns its output."""
    r = mako(ws, *args)
    assert r.code != 0, f"mako {' '.join(args)} should have failed:\n{r.text}"
    return r.text


def mcp_session(ws: Workspace, agent: str, requests: list[dict]) -> dict:
    """Send JSON-RPC requests (after initialize, id 0) to `mako mcp` as slot
    `agent`. Returns the responses by id (requests are numbered from 1)."""
    lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
    lines += [json.dumps({"jsonrpc": "2.0", "id": i, **req}) for i, req in enumerate(requests, 1)]
    r = run([sys.executable, str(MAKO), "mcp"], ws.root, {**ws.env, "MAKO_AGENT": agent},
            stdin="\n".join(lines) + "\n")
    assert r.code == 0, r.text
    return {msg["id"]: msg for msg in map(json.loads, r.out.splitlines())}


def mcp(ws: Workspace, agent: str, tool: str, **args) -> tuple[str, bool]:
    """Call one MCP tool as `agent`; returns (text, is_error)."""
    resp = mcp_session(ws, agent, [{"method": "tools/call", "params": {"name": tool, "arguments": args}}])[1]
    result = resp["result"]
    return result["content"][0]["text"], result["isError"]


def mcp_ok(ws: Workspace, agent: str, tool: str, **args) -> str:
    text, is_error = mcp(ws, agent, tool, **args)
    assert not is_error, f"{tool} failed: {text}"
    return text


def mcp_err(ws: Workspace, agent: str, tool: str, **args) -> str:
    text, is_error = mcp(ws, agent, tool, **args)
    assert is_error, f"{tool} should have failed: {text}"
    return text


def mcp_tools(ws: Workspace, agent: str) -> list[str]:
    resp = mcp_session(ws, agent, [{"method": "tools/list"}])[1]
    return [t["name"] for t in resp["result"]["tools"]]


def mcp_instructions(ws: Workspace, agent: str) -> str:
    return mcp_session(ws, agent, [])[0]["result"]["instructions"]


def set_config(ws: Workspace, **values) -> None:
    path = ws.root / ".mako" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.update(values)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


# --- simulating an agent ---------------------------------------------------------

def git(ws: Workspace, *args: str, cwd: Path | None = None) -> str:
    """git in the main checkout (or cwd); must succeed. Returns stdout, stripped."""
    r = run(["git", "-C", str(cwd or ws.repo), *args], ws.root, ws.env)
    assert r.code == 0, f"git {' '.join(args)} failed:\n{r.text}"
    return r.out.strip()


def commit(ws: Workspace, slot: str, path: str, text: str, message: str | None = None) -> str:
    """Write a file in a slot's worktree and commit it; returns the new sha."""
    wt = ws.worktree(slot)
    (wt / path).parent.mkdir(parents=True, exist_ok=True)
    (wt / path).write_text(text, encoding="utf-8")
    git(ws, "add", "--", path, cwd=wt)
    git(ws, "commit", "-q", "-m", message or f"edit {path}", cwd=wt)
    return git(ws, "rev-parse", "HEAD", cwd=wt)


def branch_exists(ws: Workspace, branch: str) -> bool:
    return bool(git(ws, "branch", "--list", branch))


def wait_for(path: Path, timeout: float = 30) -> str:
    """Wait for a file a detached process writes; returns its text."""
    end = time.time() + timeout
    while time.time() < end:
        if path.is_file() and path.stat().st_size:
            return path.read_text(encoding="utf-8")
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared")


# --- simulating the user in Obsidian ---------------------------------------------

def _board_path(ws: Workspace) -> Path:
    return ws.root / "kanban" / "board.md"


def _column_insert_at(lines: list[str], column: str) -> int:
    """Index just after a column's heading (and Done's **Complete** marker)."""
    i = lines.index(f"## {COLUMN_NAMES[column]}") + 1
    while i < len(lines) and (not lines[i].strip() or lines[i].strip() == "**Complete**"):
        i += 1
    return i


def drag(ws: Workspace, cid: str, column: str) -> None:
    """Move a card's line to the top of another column, as an Obsidian drag does."""
    lines = _board_path(ws).read_text(encoding="utf-8").split("\n")
    line = next(ln for ln in lines if f"[[{cid}]]" in ln)
    lines.remove(line)
    box = "[x]" if column == "done" else "[ ]"
    lines.insert(_column_insert_at(lines, column), re.sub(r"^- \[[ xX]\]", f"- {box}", line))
    _board_path(ws).write_text("\n".join(lines), encoding="utf-8")


def type_card(ws: Workspace, column: str, text: str) -> None:
    """A card typed by hand on the board (no id yet)."""
    lines = _board_path(ws).read_text(encoding="utf-8").split("\n")
    lines.insert(_column_insert_at(lines, column), f"- [ ] {text}")
    _board_path(ws).write_text("\n".join(lines), encoding="utf-8")


def card_file(ws: Workspace, cid: str) -> Path:
    found = list((ws.root / "kanban").glob(f"*/{cid}.md"))
    assert len(found) == 1, f"{cid}: {found}"
    return found[0]


def move_file(ws: Workspace, cid: str, folder: str) -> None:
    """A card file moved by hand between kanban/ folders."""
    src = card_file(ws, cid)
    shutil.move(str(src), str(ws.root / "kanban" / folder / src.name))


def _parse_frontmatter(text: str) -> tuple[list[str], str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text.replace("\r\n", "\n"), re.S)
    assert m, "no frontmatter"
    return m.group(1).split("\n"), m.group(2)


def edit_frontmatter(ws: Workspace, cid: str, **fields: str) -> None:
    """A hand edit of card frontmatter, as typed in Obsidian."""
    path = card_file(ws, cid)
    lines, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    for key, value in fields.items():
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}:")), None)
        if idx is None:
            lines.append(f"{key}: {value}")
        else:
            lines[idx] = f"{key}: {value}"
    path.write_text("---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8")


# --- reading state ---------------------------------------------------------------

def card(ws: Workspace, cid: str) -> dict:
    """A card's frontmatter as a dict, plus "_folder" (open/closed/discarded) and "_body"."""
    path = card_file(ws, cid)
    lines, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    meta = {"_folder": path.parent.name, "_body": body}
    for ln in lines:
        key, _, value = ln.partition(":")
        value = value.strip()
        if value.startswith('"'):
            value = json.loads(value)
        meta[key.strip()] = value
    return meta


def board(ws: Workspace) -> dict[str, list[str]]:
    """Column -> card ids (top to bottom) as board.md shows them."""
    cols: dict[str, list[str]] = {c: [] for c in COLUMN_NAMES}
    names = {v: k for k, v in COLUMN_NAMES.items()}
    column = None
    for ln in _board_path(ws).read_text(encoding="utf-8").split("\n"):
        if ln.startswith("## "):
            column = names.get(ln[3:].strip())
        elif column and (m := re.match(r"^- \[[ xX]\] .*?\[\[([A-Z]+-\d+)\]\]", ln)):
            cols[column].append(m.group(1))
    return cols


def snapshot(request, name: str, text: str) -> None:
    """Compare text with tests/snapshots/<name>; --update-snapshots rewrites it."""
    path = SNAPSHOTS / name
    text = text.replace("\r\n", "\n")
    if request.config.getoption("--update-snapshots"):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return
    assert path.is_file(), f"no snapshot {name}; run `python scripts/test.py --update-snapshots`"
    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text == expected, (f"{name} changed; if that's intended, run "
                              f"`python scripts/test.py --update-snapshots`")
