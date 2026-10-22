"""Launching the agents, and `mako init`."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from mako_core import Mako, new_session_tag
from mako_git import _git, branch_exists, commit_ident, current_branch, git, git_ok, is_broken_worktree
from mako_hooks import CONFIG_DEFAULTS
from mako_utils import MakoError


# --- launching -------------------------------------------------------------------

def safe_title(project: str) -> str:
    """The project name as it appears in pane titles. Anything cmd, a .bat or the wt
    command line treats specially (& % ^ ; | < > ! quotes, backslash...) becomes "_"."""
    return re.sub(r"[^\w .,@#+=()-]", "_", project)


def pane_title(project: str, slot: str) -> str:
    return f"{safe_title(project)} [{slot}]"


def launch(m: Mako, who: str | None, resume: bool = False) -> str:
    """Open the agents in Windows Terminal (Linux: mako.launch.sh). With resume, each one
    continues its latest conversation (agent.cmd ... resume -> claude -c)."""
    extra = ["resume"] if resume else []
    verb = "Resumed" if resume else "Opened"
    if os.name != "nt":
        if who is not None:
            raise MakoError("launching a single slot is not supported on Linux yet; "
                            "run mako.launch.sh to open them all")
        subprocess.Popen(["bash", str(m.root / "mako.launch.sh"), *extra], cwd=m.root,
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"{verb} a terminal with all agents"
    if who is None:
        subprocess.Popen(["cmd", "/c", str(m.root / "mako.launch.bat"), *extra], cwd=m.root,
                         creationflags=subprocess.DETACHED_PROCESS)
        return f"{verb} Windows Terminal with all agents"
    if who != "orchestrator" and who not in m.agents:
        raise MakoError(f"unknown slot '{who}'")
    cwd = m.root if who == "orchestrator" else m.worktree(who)
    title = pane_title(m.config["project"], who)
    subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "--suppressApplicationTitle",
                      "-d", str(cwd), "cmd", "/k", str(m.dir / "agent.cmd"), who, *extra],
                     creationflags=subprocess.DETACHED_PROCESS)
    return f"{verb} a tab for {who}"


# Claude Code cuts project folder names to this many characters (plus a hash suffix).
CLAUDE_PROJECT_NAME_MAX = 200


def has_session(cwd: Path) -> bool:
    """Whether Claude Code has a conversation that `claude -c` would continue in
    cwd. Claude keeps them as <config>/projects/<cwd, non-alphanumerics as '-'>/*.jsonl
    (long names are cut and given a hash suffix, hence the prefix match). If that
    layout ever changes this says no, and agent.cmd just starts a fresh session."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    name = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    projects = config / "projects"
    if not projects.is_dir():
        return False
    cut = CLAUDE_PROJECT_NAME_MAX
    for d in projects.iterdir():
        same = d.name == name or (len(name) > cut and d.name.startswith(name[:cut]))
        if same and any(d.glob("*.jsonl")):
            return True
    return False


# --- init ------------------------------------------------------------------------

ROOT_CMD = '@python "%~dp0.mako\\mako.py" %*\n'
BIN_CMD = '@python "%~dp0..\\mako.py" %*\n'
BIN_SH = """\
#!/bin/sh
# POSIX shim so `mako` works from Git Bash (Claude Code's shell and `!` commands).
exec python "$(dirname "$0")/../mako.py" "$@"
"""
# Linux counterparts. Not named `mako` at the root: that is a valid project name, and the
# project folder would clash. The shim is in its own folder (bin-linux) because bin/mako is
# the Git Bash one, and both OSes' files live side by side in a shared workspace.
ROOT_SH = """\
#!/bin/sh
exec python3 "$(dirname "$(readlink -f "$0")")/.mako/mako.py" "$@"
"""
BIN_SH_LINUX = """\
#!/bin/sh
# `mako` on PATH in the agent panes (agent.sh puts this folder first).
exec python3 "$(dirname "$(readlink -f "$0")")/../mako.py" "$@"
"""
# Written from templates/posix/ on Linux only: mako.launch.sh picks a terminal and runs its
# script from .mako/launchers/, which Windows has no use for (mako.launch.bat does it all).
LINUX_TERMINALS = ("konsole", "gnome-terminal", "tmux")

AGENT_CMD = """\
@echo off
rem Starts Claude for one mako slot:  agent.cmd <slot> [resume]
rem "orchestrator" runs at the workspace root; worker slots run in their worktree.
rem With "resume", Claude continues the slot's latest conversation (claude -c);
rem Claude keys conversations by directory, so each slot gets its own back.
setlocal enabledelayedexpansion
set "SLOT=%~1"
if "%SLOT%"=="" (
  echo usage: agent.cmd ^<slot^|orchestrator^> [resume]
  exit /b 1
)
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "MAKO_AGENT=%SLOT%"
set "PATH=%ROOT%\\.mako\\bin;%PATH%"
title {title} [%SLOT%]

if /i "%SLOT%"=="orchestrator" (
  set "DIR=%ROOT%"
  set "ADD=%ROOT%\\{project}"
) else (
  set "DIR=%ROOT%\\.mako\\worktrees\\%SLOT%"
  set "ADD=%ROOT%"
)
cd /d "%DIR%"

rem A worker slot whose worktree link is broken (workspace used on another OS): warn, never repair.
if /i not "%SLOT%"=="orchestrator" (
  git rev-parse --git-dir >nul 2>&1 || (
    echo WARNING: %SLOT%'s worktree is broken, probably from another OS.
    echo Run "mako repair" in the orchestrator pane.
  )
)

rem Only continue when there is a conversation to continue; otherwise start fresh.
set "CONTINUE="
if /i "%~2"=="resume" (
  python "%~dp0mako.py" session >nul 2>&1 && set "CONTINUE=-c"
)
if /i "%~2"=="resume" if not defined CONTINUE echo No earlier conversation for %SLOT%; starting a new one.

rem Name the session <project>-<slot>-<tag> so other sessions can address it unambiguously.
rem Claude drops the name on -c, so -n goes on every launch. Without a session_tag: no name.
set "SESSION_NAME="
for /f "usebackq delims=" %%N in (`python "%~dp0mako.py" session-name %SLOT% 2^>nul`) do set "SESSION_NAME=-n %%N"

rem The MCP config is built here from this script's own location, not read from a file, so a
rem workspace used from another OS never points at a path that does not exist. Forward
rem slashes keep the JSON valid; !MCP! (delayed) keeps its quotes out of cmd's parsing.
set "MAKOPY=%~dp0mako.py"
set "MAKOPY=%MAKOPY:\\=/%"
set "MCP={{\\"mcpServers\\":{{\\"mako\\":{{\\"command\\":\\"python\\",\\"args\\":[\\"%MAKOPY%\\",\\"mcp\\"],"
set "MCP=!MCP!\\"env\\":{{\\"MAKO_AGENT\\":\\"%SLOT%\\"}}}}}}}}"

claude %CONTINUE% %SESSION_NAME% --add-dir "%ADD%" ^
  --mcp-config "!MCP!" --allowedTools mcp__mako
"""

LAUNCH_BAT = """\
@echo off
rem Opens Windows Terminal with the orchestrator and the worker slots side by
rem side in equal-width columns, left to right:
rem
{diagram}
rem
rem   pane 0: orchestrator
{panes}
rem
rem Each split names the pane it splits with focus-pane, so the layout never
rem depends on which pane happens to have focus when wt processes the command.
rem
rem "mako.launch.bat resume" (mako resume) passes "resume" on to every pane's
rem agent.cmd, so each agent continues its latest conversation.

for %%I in ("%~dp0.") do set "ROOT=%%~fI"
set "WT=%ROOT%\\.mako\\worktrees"
set "AGENT=%ROOT%\\.mako\\agent.cmd"

wt -w new ^
  {commands}
"""


def render_launch_bat(project: str, agents: list[str]) -> str:
    # Pane ids follow creation order: orchestrator 0, then slot k is pane k.
    # Each slot splits the pane to its left vertically; wt's --size is the share
    # the new pane takes, so pane k-1 hands over (n-k+1)/(n-k+2) of its width,
    # leaving every column 1/(n+1) of the window.
    def pane(slot: str, cwd: str) -> str:
        return (f'--title "{pane_title(project, slot)}" --suppressApplicationTitle '
                f'-d "{cwd}" cmd /k "%AGENT%" {slot} %*')
    cmds = ["new-tab " + pane("orchestrator", "%ROOT%")]
    n = len(agents)
    for k, slot in enumerate(agents, 1):
        cmds.append(f"focus-pane -t {k - 1}")
        size = (n - k + 1) / (n - k + 2)
        cmds.append(f"split-pane -V --size {size:.3g} " + pane(slot, f"%WT%\\{slot}"))
    cmds.append("focus-pane -t 0")
    names = ["orchestrator", *agents]
    width = max(map(len, names)) + 2
    rule = "rem   +" + "+".join("-" * width for _ in names) + "+"

    def row(cells):
        return "rem   |" + "|".join(c.center(width) for c in cells) + "|"
    blank = row([""] * len(names))
    diagram = "\n".join([rule, blank, row(names), blank, rule])
    panes = "\n".join(f"rem   pane {k}: {slot}" for k, slot in enumerate(agents, 1)) or "rem   (no slots)"
    return LAUNCH_BAT.format(diagram=diagram, panes=panes, commands=" ; ^\n  ".join(cmds))


def text_sha256(data: bytes) -> str:
    """sha256 of a text file's content, ignoring line endings and outer newlines."""
    text = data.decode("utf-8").replace("\r\n", "\n").strip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def same_path(a, b) -> bool:
    return Path(a).resolve() == Path(b).resolve()


# Mako's code: the entry point mako.py and its modules, installed side by side in .mako/.
CODE_FILES = "mako*.py"


class Init:
    """`mako init`: turn a folder into a Mako workspace. Safe to re-run: it
    upgrades Mako's code in .mako/ and rewrites the generated files, but never touches
    cards, config.json, an existing CLAUDE.md or the project repo's history."""

    def __init__(self, root: Path):
        self.root = root
        self.dir = root / ".mako"
        self.log: list[str] = []

    def run(self, project, prefix, agents, base) -> str:
        config = self.config(project, prefix, agents, base)
        project, base, agents = config["project"], config["base_branch"], config["agents"]
        repo = self.root / project
        self.repo(repo, base)
        # Written only once the repo checks pass, so a failed first init can be
        # re-run with other options.
        self.dir.mkdir(exist_ok=True)
        self.write(self.dir / "config.json", json.dumps(config, indent=2) + "\n")
        for d in ("kanban/open", "kanban/closed", "kanban/discarded", ".mako/bin", ".mako/agents",
                  ".mako/locks", ".mako/worktrees"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.install()
        self.worktrees(repo, base, agents)
        self.generate(project, agents)
        self.prompts()
        self.obsidian(project)
        if not (self.root / "CLAUDE.md").exists():
            self.claude_md(project)
        Mako(self.root).sync()  # renders kanban/board.md from the cards
        self.log.append(f"\nMako workspace ready: {self.root}\n"
                        f"  Open this folder as an Obsidian vault to see kanban/board.md.\n"
                        f"  Run {'mako.launch.bat' if os.name == 'nt' else './mako.launch.sh'} to start the "
                        f"orchestrator and {', '.join(agents) or 'no slots'}.\n"
                        f"  Later, `mako resume` reopens them, each continuing its last conversation.")
        return "\n".join(self.log)

    def config(self, project, prefix, agents, base) -> dict:
        path = self.dir / "config.json"
        if path.is_file():
            config = json.loads(path.read_text(encoding="utf-8"))
            if project and project != config["project"]:
                raise MakoError(f"this workspace's project is '{config['project']}', not '{project}'")
            if any(v is not None for v in (prefix, agents, base)):
                self.log.append("kept existing .mako/config.json (edit it to change prefix, agents or base)")
            for key, value in CONFIG_DEFAULTS.items():
                config.setdefault(key, value)
            if not config.get("session_tag"):  # backfilled once; an existing tag is never changed
                config["session_tag"] = new_session_tag(self.root)
            return config
        if not project:
            raise MakoError("usage: mako init <project_name>")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project):
            raise MakoError(f"bad project name '{project}' (letters, digits, '.', '_', '-')")
        prefix = (prefix or re.sub(r"[^A-Za-z]", "", project)).upper()
        if not re.fullmatch(r"[A-Z]+", prefix):
            raise MakoError("the prefix must be letters only; pass --prefix")
        agents = [a.strip() for a in (agents or "agent-a,agent-b").split(",") if a.strip()]
        for a in agents:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", a) or a.lower() == "orchestrator":
                raise MakoError(f"bad agent slot name '{a}'")
        if len(set(agents)) != len(agents):
            raise MakoError("agent slot names must be unique")
        return {"project": project, "prefix": prefix, "base_branch": base or "main", "agents": agents,
                **CONFIG_DEFAULTS, "session_tag": new_session_tag(self.root)}

    def write(self, path: Path, data: str | bytes, crlf: bool = False) -> None:
        """Write only if the content changed, and log it."""
        if isinstance(data, str):
            data = (data.replace("\n", "\r\n") if crlf else data).encode("utf-8")
        if path.is_file() and path.read_bytes() == data:
            return
        self.log.append(f"{'updated' if path.exists() else 'created'} {path.relative_to(self.root).as_posix()}")
        path.write_bytes(data)

    def install(self) -> None:
        """Copy the running code (mako.py and the mako_*.py modules next to it) and its templates
        into .mako/. A module the new code no longer has is removed."""
        here = Path(__file__).resolve().parent
        if not same_path(here, self.dir):
            code = {p.name: p for p in sorted(here.glob(CODE_FILES))}
            for name, src in code.items():
                self.write(self.dir / name, src.read_bytes())
            for old in sorted(self.dir.glob(CODE_FILES)):
                if old.name not in code:
                    old.unlink()
                    self.log.append(f"removed .mako/{old.name}")
        tpl, dst_tpl = here / "templates", self.dir / "templates"
        if tpl.is_dir() and not same_path(tpl, dst_tpl):
            shutil.rmtree(dst_tpl, ignore_errors=True)
            shutil.copytree(tpl, dst_tpl)

    def repo(self, repo: Path, base: str) -> None:
        repo.mkdir(exist_ok=True)
        top = _git(repo, "rev-parse", "--show-toplevel")
        # A folder that merely sits inside some other repo is not adopted.
        if top.returncode != 0 or not same_path(top.stdout.strip(), repo):
            git(repo, "init", "-q")
            self.log.append(f"initialised git repo {repo.name}/")
        if not git_ok(repo, "rev-parse", "--verify", "-q", "HEAD"):
            # Worktrees need a commit. Unborn repo: put HEAD on the base branch first.
            git(repo, "symbolic-ref", "HEAD", f"refs/heads/{base}")
            git(repo, *commit_ident(repo), "commit", "-q", "--allow-empty", "-m", "Initial commit")
            self.log.append(f"made an initial commit on {base} in {repo.name}/")
        if not branch_exists(repo, base):
            raise MakoError(f"{repo.name}/ has no branch '{base}'; create it or pass --base <branch>")
        if current_branch(repo) != base:
            self.log.append(f"warning: {repo.name}/ is not on {base}; closing issues needs it there")

    def worktrees(self, repo: Path, base: str, agents: list[str]) -> None:
        # Prune deletes the admin dir of a broken worktree for good, so with any slot broken
        # (e.g. moved from another OS) it is skipped and the user is pointed at `mako repair`.
        broken = [a for a in agents if is_broken_worktree(self.dir / "worktrees" / a)]
        if broken:
            self.log.append(f"warning: worktree(s) broken: {', '.join(broken)}; skipped git worktree "
                            "prune. Run `mako repair` to fix them.")
        else:
            git(repo, "worktree", "prune")
        for slot in agents:
            wt = self.dir / "worktrees" / slot
            if (wt / ".git").exists():
                continue
            if wt.exists() and any(wt.iterdir()):
                raise MakoError(f"{wt} exists but is not a git worktree; move it aside and re-run init")
            git(repo, "worktree", "add", "-q", "--detach", str(wt), base)
            self.log.append(f"created worktree .mako/worktrees/{slot} (detached at {base})")

    def generate(self, project: str, agents: list[str]) -> None:
        """The launchers and shims for this OS only. Re-running init on the other OS adds
        that OS's files and leaves these alone, so a shared workspace ends up with both."""
        if os.name == "nt":
            self.write(self.root / "mako.cmd", ROOT_CMD, crlf=True)
            self.write(self.root / "mako.launch.bat", render_launch_bat(project, agents), crlf=True)
            self.write(self.dir / "agent.cmd", AGENT_CMD.format(project=project, title=safe_title(project)), crlf=True)
            self.write(self.dir / "bin" / "mako.cmd", BIN_CMD, crlf=True)
            self.write_script(self.dir / "bin" / "mako", BIN_SH)
            return
        self.write_script(self.root / "mako.sh", ROOT_SH)
        self.write_script(self.dir / "bin-linux" / "mako", BIN_SH_LINUX)
        posix = Path(__file__).resolve().parent / "templates" / "posix"
        scripts = [("mako.launch.sh", self.root), ("agent.sh", self.dir)]
        scripts += [(f"mako.{t}.launch.sh", self.dir / "launchers") for t in LINUX_TERMINALS]
        for name, dst in scripts:
            if (posix / name).is_file():
                # LF only: a Windows checkout with autocrlf gives these CRLF, and bash then fails on every line.
                self.write_script(dst / name, (posix / name).read_bytes().replace(b"\r\n", b"\n"))

    def write_script(self, path: Path, data: str | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write(path, data)
        os.chmod(path, 0o755)

    def prompts(self) -> None:
        """Install the default .mako/prompts/*.txt. These are the user's to edit, so an
        edited file is never overwritten. .installed.json records what Mako last wrote,
        so a file still as installed follows the shipped default when that changes."""
        tpl = Path(__file__).resolve().parent / "templates" / "prompts"
        if not tpl.is_dir():
            self.log.append(f"warning: no prompt templates at {tpl}; skipped .mako/prompts/")
            return
        folder = self.dir / "prompts"
        folder.mkdir(parents=True, exist_ok=True)
        record_path = folder / ".installed.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            record = {}
        for src in sorted(tpl.glob("*.txt")):
            dst, name = folder / src.name, f".mako/prompts/{src.name}"
            new = text_sha256(src.read_bytes())
            # "installed": what Mako last wrote; "default": the newest default seen.
            # No record (before MAKO-0014): unedited only if it is the current default.
            rec = record.get(src.name, {})
            if not dst.exists():
                self.write(dst, src.read_bytes())
                rec = {"installed": new, "default": new}
            elif (cur := text_sha256(dst.read_bytes())) == new:
                rec = {"installed": new, "default": new}
            elif cur == rec.get("installed"):
                dst.write_bytes(src.read_bytes())
                self.log.append(f"updated {name} (new default)")
                rec = {"installed": new, "default": new}
            elif rec.get("default") != new:
                # Edited by the user: kept. Said once per new default.
                why = "the default changed" if rec else "it differs from the default"
                self.log.append(f"note: {name} is edited, so it was kept; {why}, "
                                f"see .mako/templates/prompts/{src.name}")
                rec = {**rec, "default": new}
            record[src.name] = rec
        self.write(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")

    def claude_md(self, project: str) -> None:
        """A new workspace's CLAUDE.md: the line from .mako/prompts/claude-pointer.txt
        pointing agents at Mako, then a heading for the user's project text."""
        path = self.dir / "prompts" / "claude-pointer.txt"
        try:
            pointer = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
        except FileNotFoundError:
            self.log.append("warning: .mako/prompts/claude-pointer.txt is missing; "
                            "CLAUDE.md was made without the line pointing at Mako")
            self.write(self.root / "CLAUDE.md", f"# {project}\n")
            return
        self.write(self.root / "CLAUDE.md", pointer.replace("{project}", project) + f"\n\n# {project}\n")

    def obsidian(self, project: str) -> None:
        tpl = Path(__file__).resolve().parent / "templates" / "obsidian"
        vault = self.root / ".obsidian"
        if not tpl.is_dir():
            self.log.append(f"warning: no Obsidian templates at {tpl}; skipped .obsidian/")
        else:
            for src in sorted(p for p in tpl.rglob("*") if p.is_file()):
                rel = src.relative_to(tpl)
                dst = vault / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if rel.name == "community-plugins.json" and dst.is_file():
                    # Keep the user's own plugins enabled alongside ours.
                    have = json.loads(dst.read_text(encoding="utf-8"))
                    want = have + [p for p in json.loads(src.read_text(encoding="utf-8")) if p not in have]
                    if want != have:
                        self.write(dst, json.dumps(want, indent=2) + "\n")
                elif rel.parts[0] == "plugins" and rel.name != "data.json":
                    self.write(dst, src.read_bytes())  # plugin code: upgrade
                elif not dst.exists():
                    self.write(dst, src.read_bytes())  # settings: only if missing
        ignore = self.root / ".obsidianignore"
        line = f"{project}/*/"
        old = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
        if line not in old.splitlines():
            self.write(ignore, (old.rstrip("\n") + "\n" if old.strip() else "") + line + "\n")
