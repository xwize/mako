#!/usr/bin/env python3
"""mako - Multi-Agent Kanban for Obsidian.

Lives in <workspace>/.mako/mako.py. Standard library only.

Workspace layout:
    <workspace>/
        kanban/board.md         Obsidian Kanban board, regenerated from the cards
        kanban/open/            one card per open issue (<PREFIX>-0001.md)
        kanban/closed/          closed cards
        <project>/              main checkout of the code repo (base branch)
        .mako/config.json       project, prefix, base branch, agent slots
        .mako/locks/<ID>/       claim locks (mkdir is atomic)
        .mako/worktrees/<slot>/ one permanent git worktree per agent slot

Card frontmatter is the source of truth. board.md is a view of it, but drags
made in Obsidian, and card files moved between the kanban folders by hand, are
picked up by `sync`, which every command runs first.

Lifecycle: Backlog -> In Progress (claim) -> Done (close). Closing merges the
issue branch into the base branch, so nothing reaches Done unmerged. The worker
first merges the base branch into its own branch and resolves any conflicts in
its worktree; close refuses a branch that is behind the base branch. Spikes are
the exception: they close without merging and their branch is kept.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime
from functools import lru_cache
from pathlib import Path

VERSION = "0.1.0"
TYPES = ("feature", "bug", "spike")
COLUMNS = {"backlog": "Backlog", "in-progress": "In Progress", "done": "Done"}
STATES = {**COLUMNS, "discarded": "Discarded"}
FIELDS = ("id", "type", "title", "depends_on", "status", "created", "branch",
          "claimed_by", "claimed", "merged", "closed", "discarded", "reviews")
LIST_FIELDS = ("depends_on",)


class MakoError(Exception):
    pass


def now() -> str:
    return os.environ.get("MAKO_NOW") or datetime.now().strftime("%Y-%m-%d %H:%M")  # MAKO_NOW: tests


# Review backends. "review": {"backend": "<name>", ...} in config.json loads the workspace's
# .mako/templates/review/<name>.py (shipped in templates/review/, so mako.py stays one file). Its
# hooks get a context dict (see Mako.review_context) and raise the module's ReviewError for a
# message to show; Mako calls them, agents never do. Only request is required:
#   setup(ctx) -> str                                 `mako review setup`
#   request(ctx, issue) -> {"text": .., "ref": .., "url": .., "revision": ..}
#                                                     request_review; issue: id, title, branch, agent, sha.
#                                                     A ref (not None) marks the issue as sent to it. ref
#                                                     and url reach review_hook as {review_ref}, {review_url}.
#                                                     revision (optional) names what was uploaded (e.g. a
#                                                     patch set); Mako keeps it and passes it to comment.
#   status(ctx, issue_id) -> dict                     review_status, `mako review <ID> --status`
#   comment(ctx, issue, review) -> {"text": .., "url": ..}
#                                                     post_review, `mako review <ID> --comment`: another
#                                                     slot (or the human) reviews the uploaded revision.
#                                                     issue: id, title, branch, agent (the author), sha,
#                                                     ref, revision. review: reviewer, message, score,
#                                                     comments [{"path", "line" (None: the file), "message"}].
#                                                     Mako has checked the sha, the reviewer, the score and
#                                                     every path and line before calling it.
#   finish(ctx, issue, event, outcome, note) -> dict  after close/release/discard (event) of an issue with
#                                                     a ref; on close, {"transcript": markdown} is kept
#                                                     as <review_transcripts_to>/review_<ID>.md if that
#                                                     config setting names a folder (default: not kept)


@lru_cache(maxsize=None)
def load_review_backend(path: Path):
    if not path.is_file():
        raise MakoError(f"unknown review backend '{path.stem}' in .mako/config.json: there is no {path} "
                        "(`mako init` installs the shipped ones)")
    spec = importlib.util.spec_from_file_location(f"mako_review_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    was, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in .mako/templates
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = was
    return module


def _config_int(config: dict, key: str) -> int:
    """A non-negative integer setting; missing, malformed or negative values fall back to the default."""
    default = CONFIG_DEFAULTS[key]
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def minutes_between(start: str, end: str) -> float | None:
    """Minutes from one card timestamp to another, or None if either doesn't parse."""
    try:
        fmt = "%Y-%m-%d %H:%M"
        return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "issue"


def write_atomic(path: Path, text: str) -> None:
    # Dot-prefixed temp file so Obsidian never indexes it. Retry the replace
    # because Obsidian can briefly hold the target open on Windows.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # write_text(newline=) is 3.10+
        f.write(text)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


# --- git -------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    # stdin is DEVNULL so git can never eat the MCP server's stdin.
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, check=False)


def git(cwd: Path, *args: str) -> str:
    r = _git(cwd, *args)
    if r.returncode != 0:
        raise MakoError((r.stderr or r.stdout).strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def git_ok(cwd: Path, *args: str) -> bool:
    return _git(cwd, *args).returncode == 0


def git_file(cwd: Path, rev: str, path: str) -> bytes | None:
    """A file's bytes at rev (None if it isn't there). Bytes, so the text isn't re-encoded."""
    r = subprocess.run(["git", "-C", str(cwd), "show", f"{rev}:{path}"], capture_output=True,
                       stdin=subprocess.DEVNULL, check=False)
    return r.stdout if r.returncode == 0 else None


def commit_ident(cwd: Path) -> list[str]:
    """git options that make Mako the committer when the repo has no user.email of its own."""
    if _git(cwd, "config", "user.email").stdout.strip():
        return []
    return ["-c", "user.name=Mako", "-c", "user.email=mako@localhost"]


def commit_file(wt: Path, path: str, data: bytes, message: str) -> None:
    """Write a file in a worktree and commit just that file (Mako's own commits)."""
    target = wt / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    git(wt, "add", "--", path)
    git(wt, *commit_ident(wt), "commit", "-q", "-m", message, "--", path)


def is_clean(cwd: Path) -> bool:
    return git(cwd, "status", "--porcelain") == ""


def is_broken_worktree(wt: Path) -> bool:
    """True when wt has a .git link that git can't follow (read-only check). Typically the
    workspace was created on another OS, so the absolute paths git wrote are wrong here."""
    return (wt / ".git").exists() and not git_ok(wt, "rev-parse", "--git-dir")


def current_branch(cwd: Path) -> str | None:
    """None when HEAD is detached."""
    return _git(cwd, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or None


def branch_exists(cwd: Path, branch: str) -> bool:
    return git_ok(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")


def unmerged_commits(cwd: Path, branch: str, base: str) -> int:
    """Commits on branch that are not in base (0 if the branch doesn't exist)."""
    if not branch or not branch_exists(cwd, branch):
        return 0
    return int(git(cwd, "rev-list", "--count", f"{base}..{branch}"))


def check_merge_to(cwd: Path, merge_to: str, kind: str | None) -> str:
    """A merge_to value cleaned up, or MakoError. Empty means the base branch."""
    merge_to = merge_to.strip()
    if merge_to and kind == "spike":
        raise MakoError("merge_to is not for spikes (they never merge)")
    if merge_to and not git_ok(cwd, "check-ref-format", "--branch", merge_to):
        raise MakoError(f"merge_to '{merge_to}' is not a valid branch name")
    return merge_to


def start_shell(cmd: str, cwd: Path, out, env: dict | None, flags: int = 0) -> subprocess.Popen:
    """Start a shell command in a process group of its own. Through the shell so things like
    npx (a .cmd on Windows) resolve; stdin is DEVNULL so it can never read the MCP server's
    stdin, and output goes to `out`. On Windows it gets a hidden console of its own (console
    children stay hidden too), plus any extra creation flags."""
    kw = dict(cwd=str(cwd), shell=True, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env)
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=True, **kw)
    return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                            | flags, **kw)


def spawn_detached(cmd: str, cwd: Path, out, env: dict | None = None) -> None:
    """Start a shell command and don't wait for it. On Windows it leaves the caller's job
    if allowed, so it outlives the MCP server."""
    if os.name != "nt":
        start_shell(cmd, cwd, out, env)
        return
    try:
        start_shell(cmd, cwd, out, env, subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:  # the job doesn't allow breakaway
        start_shell(cmd, cwd, out, env)


def run_and_wait(cmd: str, cwd: Path, out, env: dict | None, timeout: float | None) -> int | None:
    """Run a shell command to the end (see start_shell). Returns its exit code, or None if it
    ran past timeout (seconds), when its whole process tree is killed."""
    proc = start_shell(cmd, cwd, out, env)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            with suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        return None


# --- hooks -------------------------------------------------------------------
# The user's own commands at points in an issue's life: "<event>_hook" in config.json, a command
# string or a list of them, each launched detached. Parameters reach a command as {name}
# placeholders and as MAKO_<NAME> environment variables (see Mako.run_hook).
# pre_close_hook is the exception: a gate, run and waited for before close merges (see
# Mako.run_pre_close), with an optional pre_close_timeout in seconds.

HOOK_EVENTS = ("new", "claim", "review", "pre_close", "close")
HOOK_PARAMS = ("id", "title", "type", "base", "branch", "sha", "agent", "root", "repo", "worktree",
               "review_ref", "review_url")
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The optional config.json settings and their defaults. Init writes them all, and adds any
# missing to an older config, so they can be found; it never changes a value.
CONFIG_DEFAULTS = {**{f"{event}_hook": "" for event in HOOK_EVENTS}, "hook_params": {},
                   "pre_close_timeout": 0, "keep_branches": False, "spike_publish_to": "",
                   "review_transcripts_to": "", "auto_approve": False,
                   "warn_after_minutes": 30, "warn_after_reviews": 2}


def hook_commands(value) -> list[str]:
    """A hook setting as its list of commands: a string is one, blanks are dropped."""
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if isinstance(item, str) and item.strip()]


def expand_placeholders(text: str, values: dict, unknown: list) -> str:
    """{name} replaced by values[name] in one pass (inserted text is not expanded again);
    {{ and }} are literal braces. An unknown {name} is left as it is and added to unknown."""
    def sub(match):
        name = match.group(1)
        if name is None:
            return match.group(0)[0]
        if name in values:
            return values[name]
        if name not in unknown:
            unknown.append(name)
        return match.group(0)
    return PLACEHOLDER.sub(sub, text)


# --- cards -----------------------------------------------------------------

class Card:
    def __init__(self, path: Path, meta: dict, body: str):
        self.path, self.meta, self.body = path, meta, body

    @property
    def id(self) -> str:
        return self.meta.get("id") or self.path.stem

    @property
    def status(self) -> str:
        return self.meta.get("status", "backlog")

    @property
    def title(self) -> str:
        return self.meta.get("title", "")

    @property
    def human(self) -> bool:
        """The user's own task (frontmatter `human: true`): outside the agent workflow,
        it moves freely between columns and folders and agents can't work on it."""
        return str(self.meta.get("human", "")).strip().lower() in ("true", "yes", "1")

    @property
    def approved(self) -> bool:
        """Frontmatter `approved`: cards a worker slot created stay unapproved (`false`) until the
        user ticks it. Cards from the orchestrator or the user are `true`, and a card without the
        field (older) counts as approved."""
        return str(self.meta.get("approved", "")).strip().lower() not in ("false", "no", "0")

    def render(self) -> str:
        keys = list(FIELDS) + [k for k in self.meta if k not in FIELDS]
        lines = ["---"] + [f"{k}: {_yaml_value(self.meta.get(k, ''))}" for k in keys] + ["---"]
        return "\n".join(lines) + "\n" + self.body


def _yaml_value(value) -> str:
    if isinstance(value, list):
        return f"[{', '.join(value)}]" if value else ""
    v = "" if value is None else str(value)
    if v and (re.search(r"[:#\[\]{}\"'\\,&*!|>%@`]", v) or v != v.strip()):
        return json.dumps(v, ensure_ascii=False)
    return v


def read_card(path: Path) -> Card:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return Card(path, {"id": path.stem}, text)
    meta, key = {}, None
    for line in m.group(1).splitlines():
        # Block lists, as Obsidian's Properties editor writes them: "key:" then "  - item".
        if key in LIST_FIELDS and (item := re.match(r"^\s*-\s*(.*?)\s*$", line)):
            if not isinstance(meta.get(key), list):
                meta[key] = []
            if item.group(1):
                meta[key].append(item.group(1).strip("\"'"))
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep:
            key = None
            continue
        value = value.strip()
        if key in LIST_FIELDS and value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            continue
        if value.startswith('"'):
            with suppress(ValueError):
                value = json.loads(value)
        meta[key] = value
    return Card(path, meta, m.group(2))


CARD_BODY = """
# {title}

## Description

{description}

## Acceptance criteria

{criteria}

## Notes

"""
EMPTY_CRITERIA = "- [ ]"

# The `## ` headings Mako writes in a card body: CARD_BODY's sections, and the notes
# _append adds (`## Released (agent-a, <date>)`). Only these end the Description section,
# so a description may have `## ` headings of its own, just not one of these. Like
# Markdown, a heading may be indented up to 3 spaces (4 is a code block).
CARD_SECTIONS = ("Description", "Acceptance criteria", "Notes")
NOTE_HEADINGS = ("Closing note", "Released", "Discarded", "Reopened")
MAKO_HEADING = re.compile(rf" {{0,3}}## (?:(?:{'|'.join(map(re.escape, CARD_SECTIONS))})"
                          rf"|(?:{'|'.join(map(re.escape, NOTE_HEADINGS))}) \(.*\))[ \t]*")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def mako_headings(text: str) -> list[tuple[int, str]]:
    """(offset, heading) of each Mako heading line in text, skipping fenced code blocks."""
    found, fence, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        marker = FENCE.match(bare)
        if fence:
            if (marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence)
                    and not bare[marker.end():].strip()):
                fence = None
        elif marker:
            fence = marker.group(1)
        elif MAKO_HEADING.fullmatch(bare):
            found.append((pos, bare.strip()))
        pos += len(line)
    return found


def check_description(text: str) -> None:
    """Refuse a description that would split the card: one with a Mako heading in it."""
    for _, heading in mako_headings(text):
        name = heading[3:]
        kind = "a card section" if name in CARD_SECTIONS else "a heading Mako uses for notes"
        hint = (", or fill in the card's own section" if name in CARD_SECTIONS[1:] else "")
        if name == "Acceptance criteria":
            hint = ", or pass the criteria as acceptance_criteria (--criteria) so they fill the card's own section"
        raise MakoError(f"'{heading}' is {kind}; use ### inside the description{hint}")


CRITERIA_HEADING = "## Acceptance criteria"
CHECKBOX = re.compile(r"\s*[-*+] \[[ xX]\](?: |$)")
BULLET = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+")


def criteria_lines(lines) -> str:
    """Acceptance-criteria text as checkbox lines: existing `- [ ]`/`- [x]` lines stay as they
    are, any other non-empty line (bulleted or not) becomes `- [ ] <text>`."""
    out = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        out.append(line.lstrip() if CHECKBOX.match(line) else f"- [ ] {BULLET.sub('', line, count=1).strip()}")
    return "\n".join(out) or EMPTY_CRITERIA


def split_criteria(description: str) -> tuple[str, str | None]:
    """Split a `## Acceptance criteria` section out of a description: (the description without
    it, the criteria as checkbox lines), or (description, None) when it has none. The section
    runs to the end of the text; any other Mako heading is still refused."""
    found = [pos for pos, heading in mako_headings(description) if heading == CRITERIA_HEADING]
    if not found:
        check_description(description)
        return description, None
    before, section = description[:found[0]], description[found[0]:]
    check_description(before)
    after = section.split("\n", 1)[1] if "\n" in section else ""
    check_description(after)
    return before, criteria_lines(after.splitlines())


def set_section(body: str, cid: str, name: str, text: str) -> str:
    """body with the text under its `## <name>` heading replaced (up to the next Mako heading)."""
    heading = f"## {name}"
    headings = mako_headings(body)
    starts = [i for i, (_, found) in enumerate(headings) if found == heading]
    if not starts:
        raise MakoError(f"{cid} has no '{heading}' section to replace; "
                        "fix the card by hand, or use --append")
    if starts[0] + 1 == len(headings):
        raise MakoError(f"{cid}'s {name} section has no Mako heading after it "
                        f"({', '.join('## ' + s for s in CARD_SECTIONS[1:])}), so "
                        "its end is unclear; fix the card by hand, or use --append")
    begin = body.index("\n", headings[starts[0]][0]) + 1
    return f"{body[:begin]}\n{text}\n\n{body[headings[starts[0] + 1][0]:]}"

# A spike's summary: on its branch at .mako_spikes/spike_<ID>_<slug>.md, written by Mako
# at claim time (frontmatter filled in), the body by the agent.
SPIKE_DIR = ".mako_spikes"
SUMMARY_FIELDS = ("id", "title", "agent", "created", "branch", "closed")
SUMMARY_BODY = """
# {title}

## Summary

_TBD_

## Findings

## Recommendation
"""


def cycle_from(graph: dict, start: str) -> list[str] | None:
    """A path start -> ... -> start through graph (id -> dependency ids), if there is one."""
    seen, stack = set(), [(start, [start])]
    while stack:
        node, path = stack.pop()
        for d in graph.get(node, []):
            if d == start:
                return path + [d]
            if d not in seen:
                seen.add(d)
                stack.append((d, path + [d]))
    return None


# --- workspace ---------------------------------------------------------------

def find_root(start: Path | None = None) -> Path:
    if os.environ.get("MAKO_ROOT"):
        return Path(os.environ["MAKO_ROOT"]).resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / ".mako" / "config.json").is_file():
            return d
    # Fall back to the workspace this script is vendored into.
    d = Path(__file__).resolve().parent.parent
    if (d / ".mako" / "config.json").is_file():
        return d
    raise MakoError("not inside a mako workspace (no .mako/config.json found)")


def render_session_name(config: dict, slot: str) -> str:
    """The Claude session name for a slot: <project>-<slot>-<tag>, e.g. mako_dev-agent-a-3f9c.
    The only place the format lives. Claude Code names a session with `claude -n`, and
    ListAgents / SendMessage address it by that name, so it must be unique across the
    workspaces on a machine (the tag) and free of characters SendMessage's `to` can't take."""
    tag = str(config.get("session_tag") or "").strip()
    if not tag:
        raise MakoError("this workspace has no session_tag; run `mako init` to add one")
    project = re.sub(r"[^a-z0-9_-]", "-", str(config["project"]).lower())
    return f"{project}-{re.sub(r'[^A-Za-z0-9_-]', '-', slot)}-{tag}"


def new_session_tag(root: Path) -> str:
    """4 hex chars: a hash of the workspace path plus a random salt. Generated once and kept
    in config.json, so names stay stable across restarts and when the folder is moved."""
    return hashlib.sha256(f"{root}{secrets.token_hex(8)}".encode()).hexdigest()[:4]


class Mako:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / ".mako"
        self.config = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.project = root / self.config["project"]
        self.prefix = self.config["prefix"].upper()
        self.base = self.config.get("base_branch", "main")
        self.agents = list(self.config.get("agents", []))
        # Hooks: <event>_hook -> its commands.
        self.hooks = {event: hook_commands(self.config.get(f"{event}_hook")) for event in HOOK_EVENTS}
        params = self.config.get("hook_params")
        self.hook_params = params if isinstance(params, dict) else {}
        # Seconds a pre_close_hook command may run before it is killed and the close refused. 0: no limit.
        self.pre_close_timeout = _config_int(self.config, "pre_close_timeout")
        # "review": {"backend": <name>, ..its options..} picks a review backend. Without one, a
        # review_hook is the review step and no backend code is ever loaded; with one, the
        # backend is the review step and the hook runs as well.
        review = self.config.get("review")
        self.review_options = review if isinstance(review, dict) else {}
        self.review_backend = str(self.review_options.get("backend") or "").strip().lower()
        # With neither a review_hook nor a backend the review step is manual (request_review tells
        # the agent to show the user the change), unless auto_approve turns it off. A configured
        # review step is an explicit choice, so auto_approve is ignored alongside one.
        self.auto_approve = self.config.get("auto_approve") is True
        # On: close keeps the merged branch and names it in the merge commit. Off: it's deleted.
        self.keep_branches = self.config.get("keep_branches") is True
        # Pre-fills publish_to on new spikes. Empty: no default.
        self.spike_publish_to = str(self.config.get("spike_publish_to") or "").strip()
        # Folder (relative: from the workspace root) where close keeps a review backend's transcript.
        # Empty: none is kept.
        self.review_transcripts_to = str(self.config.get("review_transcripts_to") or "").strip()
        # /clear nudge: closing an issue open longer than warn_after_minutes or reviewed more than
        # warn_after_reviews times makes that agent's next claim refuse once. 0 turns a test off.
        self.warn_after_minutes = _config_int(self.config, "warn_after_minutes")
        self.warn_after_reviews = _config_int(self.config, "warn_after_reviews")
        self.kanban = root / "kanban"
        self.open_dir = self.kanban / "open"
        self.closed_dir = self.kanban / "closed"
        self.discarded_dir = self.kanban / "discarded"
        self.card_dirs = (self.open_dir, self.closed_dir, self.discarded_dir)
        self.board = self.kanban / "board.md"
        self.locks = self.dir / "locks"
        self.notes: list[str] = []
        self.order: list[str] = []
        self.settings: str | None = None
        for d in (*self.card_dirs, self.locks):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def review_configured(self) -> bool:
        """Is a review tool set up (review_hook or a review backend)?"""
        return bool(self.hooks["review"] or self.review_backend)

    @property
    def reviewing(self) -> bool:
        """Is there a review step (request_review, and its gate on close_issue)? Without a
        configured one it is the manual step, unless auto_approve is on."""
        return self.review_configured or not self.auto_approve

    @property
    def manual_review(self) -> bool:
        """Is the review step manual (the agent shows the user the change itself)?"""
        return not self.review_configured and not self.auto_approve

    def backend_hook(self, name: str):
        """The backend's hook `name`, or None (no backend, no such hook, or it can't be loaded)."""
        if not self.review_backend:
            return None
        try:
            return getattr(self.backend(), name, None)
        except MakoError:
            return None

    @property
    def review_has_status(self) -> bool:
        """Can the backend report on a review (review_status)?"""
        return self.backend_hook("status") is not None

    @property
    def review_has_comment(self) -> bool:
        """Can other slots post reviews to the backend (post_review)?"""
        return self.backend_hook("comment") is not None

    # ids & paths

    def norm_id(self, raw: str) -> str:
        m = re.fullmatch(r"\s*(?:([A-Za-z]+)-)?0*(\d+)\s*", str(raw))
        if not m or (m.group(1) and m.group(1).upper() != self.prefix):
            raise MakoError(f"bad issue id '{raw}' (expected {self.prefix}-0001)")
        return f"{self.prefix}-{int(m.group(2)):04d}"

    def worktree(self, agent: str) -> Path:
        return self.dir / "worktrees" / agent

    def broken_slots(self) -> list[str]:
        return [a for a in self.agents if is_broken_worktree(self.worktree(a))]

    def repair(self) -> list[str]:
        """The only place Mako runs `git worktree repair`, and only when the user asks.
        Paths must be absolute; relative ones resolve against the repo, not the cwd."""
        before = set(self.broken_slots())
        existing = [self.worktree(a) for a in self.agents if (self.worktree(a) / ".git").exists()]
        if existing:
            r = _git(self.project, "worktree", "repair", *map(str, existing))
            if r.returncode != 0:
                raise MakoError((r.stderr or r.stdout).strip() or "git worktree repair failed")
        after = set(self.broken_slots())
        lines = []
        for a in self.agents:
            if a in after:
                lines.append(f"{a}: still broken")
            elif a in before:
                lines.append(f"{a}: repaired")
            elif a in {self.worktree(x).name for x in existing}:
                lines.append(f"{a}: ok")
            else:
                lines.append(f"{a}: no worktree (run mako init)")
        if after:
            raise MakoError("could not repair: " + ", ".join(sorted(after)) + "\n" + "\n".join(lines))
        return lines

    def agent_from_cwd(self) -> str | None:
        wt_root = os.path.normcase(str(self.dir / "worktrees"))
        cwd = os.path.normcase(str(Path.cwd().resolve()))
        if cwd.startswith(wt_root + os.sep):
            return Path(cwd[len(wt_root) + 1:]).parts[0]
        return None

    def card_path(self, cid: str) -> Path | None:
        for d in self.card_dirs:
            p = d / f"{cid}.md"
            if p.is_file():
                return p
        return None

    def get(self, raw: str) -> Card:
        cid = self.norm_id(raw)
        p = self.card_path(cid)
        if not p:
            raise MakoError(f"{cid} not found")
        return read_card(p)

    def cards(self) -> list[Card]:
        """Every card, discarded ones included (status "discarded", never on the board)."""
        paths = [p for d in self.card_dirs for p in sorted(d.glob(f"{self.prefix}-*.md"))]
        return [read_card(p) for p in paths]

    def next_id(self) -> str:
        nums = [int(m.group(1)) for d in self.card_dirs
                for p in d.glob(f"{self.prefix}-*.md")
                if (m := re.fullmatch(rf"{self.prefix}-(\d+)", p.stem))]
        return f"{self.prefix}-{max(nums, default=0) + 1:04d}"

    def folder_for(self, status: str) -> Path:
        return {"done": self.closed_dir, "discarded": self.discarded_dir}.get(status, self.open_dir)

    def summary_path(self, card: Card) -> str:
        """A spike's summary file, relative to the repo (fixed at its first claim)."""
        return card.meta.get("summary") or f"{SPIKE_DIR}/spike_{card.id}_{slugify(card.title)}.md"

    def publish_target(self, card: Card) -> Path | None:
        """Where closing copies a spike's summary: <publish_to>/<summary file name>,
        relative paths against the workspace root. None when publish_to is empty."""
        folder = str(card.meta.get("publish_to") or "").strip()
        if not folder:
            return None
        base = Path(folder)
        return (base if base.is_absolute() else self.root / base) / Path(self.summary_path(card)).name

    def save(self, card: Card) -> None:
        """Write the card into the folder for its status: open/, closed/ or discarded/."""
        target = self.folder_for(card.status) / f"{card.id}.md"
        write_atomic(target, card.render())
        if card.path != target and card.path.exists():
            card.path.unlink()
        card.path = target

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    # dependencies

    def parse_ids(self, raw) -> list[str]:
        """IDs from a list or a comma/space separated string, normalised and de-duplicated."""
        items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
        out = []
        for item in items:
            if str(item).strip() and (cid := self.norm_id(item)) not in out:
                out.append(cid)
        return out

    def deps(self, card: Card) -> list[str]:
        """The card's dependencies as written, tolerating hand edits in Obsidian."""
        raw = card.meta.get("depends_on") or []
        items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw).strip("[]"))
        out = []
        for item in items:
            if not str(item).strip():
                continue
            try:
                cid = self.norm_id(item)
            except MakoError:
                cid = str(item).strip()
            if cid not in out:
                out.append(cid)
        return out

    def blockers(self, card: Card) -> list[str]:
        """Dependencies that are not Done yet, as 'ID (state)'."""
        out = []
        for d in self.deps(card):
            path = self.card_path(d)
            if not path:
                out.append(f"{d} (missing)")
            elif (st := read_card(path).status) != "done":
                out.append(f"{d} ({COLUMNS.get(st, st)})")
        return out

    def dependents(self, cid: str) -> list[Card]:
        return [c for c in self.cards() if cid in self.deps(c)]

    def check_deps(self, cid: str, deps: list[str]) -> None:
        """Reject unknown ids, self-dependencies, new dependencies on discarded
        issues (they would never be Done) and cycles."""
        path = self.card_path(cid)
        current = self.deps(read_card(path)) if path else []
        for d in deps:
            if d == cid:
                raise MakoError(f"{cid} can't depend on itself")
            if not (dpath := self.card_path(d)):
                raise MakoError(f"{d} not found; {cid} can't depend on it")
            if d not in current and read_card(dpath).status == "discarded":
                raise MakoError(f"{d} is discarded; {cid} can't depend on it (`mako reopen {d}` first)")
        graph = {c.id: self.deps(c) for c in self.cards()}
        graph[cid] = deps
        if cycle := cycle_from(graph, cid):
            raise MakoError(f"that would make a dependency cycle: {' -> '.join(cycle)}")

    def dep_problems(self) -> list[str]:
        """Bad dependencies typed into cards by hand (missing ids, cycles)."""
        cards = self.cards()
        graph = {c.id: self.deps(c) for c in cards}
        problems = []
        for c in cards:
            if c.status in ("done", "discarded"):
                continue
            if missing := [d for d in graph[c.id] if d not in graph]:
                problems.append(f"{c.id} depends on unknown {', '.join(missing)}")
            if path := cycle_from(graph, c.id):
                problems.append(f"{c.id} is in a dependency cycle: {' -> '.join(path)}")
        return problems

    # locks

    def warn_flag(self, agent: str) -> Path:
        return self.dir / "warn" / agent

    def _set_warn_flag(self, card: Card, agent: str) -> None:
        """After a close: flag the agent if the issue was open too long or reviewed too often."""
        minutes = minutes_between(card.meta.get("claimed", ""), card.meta.get("closed", ""))
        slow = self.warn_after_minutes and minutes is not None and minutes > self.warn_after_minutes
        reviewed = self.warn_after_reviews and review_count(card) > self.warn_after_reviews
        if agent in self.agents and (slow or reviewed):
            flag = self.warn_flag(agent)
            flag.parent.mkdir(exist_ok=True)
            flag.write_text(now(), encoding="utf-8")

    def read_lock(self, cid: str) -> dict | None:
        d = self.locks / cid
        if not d.is_dir():
            return None
        try:
            return json.loads((d / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"id": cid, "agent": "?"}

    def all_locks(self) -> list[dict]:
        # A lock can vanish between listing and reading it (a close elsewhere): skipped.
        locks = (self.read_lock(d.name) for d in sorted(self.locks.iterdir())
                 if d.is_dir() and not d.name.startswith("_"))
        return [lk for lk in locks if lk is not None]

    def lock_of(self, agent: str) -> dict | None:
        return next((lk for lk in self.all_locks() if lk.get("agent") == agent), None)

    @contextmanager
    def board_lock(self, timeout: float = 30, stale: float = 120):
        d = self.locks / "_board"
        start = time.time()
        while True:
            try:
                d.mkdir()
                break
            except FileExistsError:
                try:
                    if time.time() - d.stat().st_mtime > stale:
                        shutil.rmtree(d, ignore_errors=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - start > timeout:
                    raise MakoError("timed out waiting for .mako/locks/_board "
                                    "(delete it if no mako command is running)") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def transact(self, fn):
        """Run fn under the board lock: sync from board.md, mutate, re-render."""
        with self.board_lock():
            self.notes = []
            self.sync_board()
            try:
                return fn()
            finally:
                self.render_board()

    # board

    def parse_board(self) -> list[tuple[str | None, str | None, str]]:
        """Returns (status, card id or None, text) for each card line."""
        if not self.board.is_file():
            return []
        text = self.board.read_text(encoding="utf-8").replace("\r\n", "\n")
        m = re.search(r"%% kanban:settings\s*```\s*(.*?)```\s*%%", text, re.S)
        self.settings = m.group(1).strip() if m else None
        if m:
            text = text[:m.start()]
        by_name = {name.lower(): status for status, name in COLUMNS.items()}
        id_re = re.compile(rf"\[\[(?:[^\]|]*/)?({re.escape(self.prefix)}-\d+)", re.I)
        items, column = [], None
        for line in text.split("\n"):
            if h := re.match(r"^##\s+(.+?)\s*$", line):
                column = by_name.get(h.group(1).lower())
                continue
            if (it := re.match(r"^- \[[ xX]\]\s*(.*)$", line)) and column:
                content = it.group(1).strip()
                if content:
                    mm = id_re.search(content)
                    items.append((column, mm.group(1).upper() if mm else None, content))
        return items

    def sync_board(self) -> None:
        """Apply what the user did by hand, in a fixed order:
        1. card files moved between kanban/ folders, judged against each card's
           status as stored (before board.md is read);
        2. board.md: drags and newly typed cards;
        3. every change goes through save(), so afterwards each card file is in
           the folder for its status.
        A card both moved as a file and dragged on the board in the same sync
        follows the file move (folder wins); its drag is ignored and the board
        is re-rendered from the cards."""
        moved = self.sync_folders()
        self.order, seen = [], set()
        for column, cid, text in self.parse_board():
            if cid is None:
                card = self._create_from_text(text, column)
                self.order.append(card.id)
                continue
            if cid in seen:
                continue
            seen.add(cid)
            self.order.append(cid)
            if cid in moved:
                continue
            path = self.card_path(cid)
            if not path:
                self.note(f"board: {cid} has no card file, removed from the board")
                continue
            card = read_card(path)
            if card.status == "discarded":
                self.note(f"board: {cid} is discarded, removed from the board "
                          f"(`mako reopen {cid}` brings it back)")
                continue
            if card.status == column:
                continue
            try:
                self._apply_drag(card, column)
                self.note(f"board: {cid} moved to {COLUMNS[column]}")
            except MakoError as e:
                self.note(f"board: can't move {cid} to {COLUMNS[column]}: {e} (moved it back)")
        for problem in self.dep_problems():
            self.note(f"warning: {problem}")

    def _apply_drag(self, card: Card, column: str) -> None:
        """A board drag. Claiming (-> In Progress) and closing (-> Done) belong to
        agents; by hand a normal card can only be released, reopened or reordered.
        Human cards go anywhere."""
        if card.human:
            self._set_human_status(card, column)
            return
        if column == "in-progress":
            raise MakoError(f"{card.id} can only be claimed by an agent (claim_issue / mako claim)")
        if column == "backlog":
            if card.status == "done":
                self._reopen(card.id)
            else:
                self._release(card.id, agent=None, note="", force=False)
        elif card.status == "in-progress":
            raise MakoError(f"{card.id} can only be closed by an agent (close_issue / mako close)")
        else:
            raise MakoError(f"a {COLUMNS[card.status]} card can't move to {COLUMNS[column]}")

    def _set_human_status(self, card: Card, status: str) -> None:
        """Move a human card: only its status and timestamps change, no locks or git."""
        card.meta.update(status=status, closed=now() if status == "done" else "",
                         discarded=now() if status == "discarded" else "")
        self.save(card)

    def sync_folders(self) -> set[str]:
        """Act on card files moved between kanban/open, closed and discarded by
        hand: the folder is the user asking for the matching action. Refused or
        failed moves snap back. Returns the ids moved (or refused), so the board
        sync leaves them alone. An id found in two folders is reported and left
        as it is."""
        found: dict[str, list[Path]] = {}
        for d in self.card_dirs:
            for p in sorted(d.glob(f"{self.prefix}-*.md")):
                found.setdefault(p.stem, []).append(p)
        handled = set()
        for cid, paths in found.items():
            if len(paths) > 1:
                where = ", ".join(f"kanban/{p.parent.name}" for p in paths)
                self.note(f"warning: {cid} is in more than one folder ({where}); neither copy was "
                          "acted on. Delete or merge the extra one by hand")
                handled.add(cid)
                continue
            card = read_card(paths[0])
            folder = paths[0].parent
            if folder == self.folder_for(card.status):
                continue
            handled.add(cid)
            try:
                self._apply_file_move(card, folder)
                state = self.get(cid).status
                self.note(f"kanban: {cid} moved to kanban/{folder.name}: now {STATES.get(state, state)}")
            except MakoError as e:
                card = read_card(self.card_path(cid) or paths[0])
                self.save(card)  # snap back into the folder for its status
                self.note(f"kanban: can't move {cid} to kanban/{folder.name}: {e} "
                          f"(moved it back to kanban/{card.path.parent.name})")
        return handled

    def _apply_file_move(self, card: Card, folder: Path) -> None:
        """A card file found in `folder`, which disagrees with its status: the same
        action as the matching command, run as the human."""
        target = {self.closed_dir: "done", self.discarded_dir: "discarded"}.get(folder, "backlog")
        if card.human:
            self._set_human_status(card, target)
        elif target == "discarded":
            self._discard(card.id, None, f"Moved to kanban/{folder.name} by hand.", force=False)
        elif target == "backlog":
            self._reopen(card.id, note=f"Moved to kanban/{folder.name} by hand.")
        elif card.status == "in-progress":
            raise MakoError(f"{card.id} can only be closed by an agent (close_issue / mako close)")
        elif card.status == "discarded":
            raise MakoError(f"{card.id} is discarded; reopen it first (move it to kanban/open)")
        else:
            raise MakoError(f"a {STATES.get(card.status, card.status)} card can't move to Done")

    def _create_from_text(self, text: str, column: str = "backlog") -> Card:
        tag = re.search(r"\s#(feature|bug|spike)\b", text)
        human = bool(re.search(r"(^|\s)#human\b", text))
        text = re.sub(r"(^|\s+)#(feature|bug|spike|human)\b", "", text).strip()
        prefix = re.match(r"(?i)^(feature|bug|spike)\s*[:\-]\s*(.+)$", text)
        if prefix:
            kind, title = prefix.group(1).lower(), prefix.group(2)
        else:
            kind, title = (tag.group(1) if tag else "feature"), text
        card = self._new(kind, title.strip(), "", human=human)
        if human and column != "backlog":
            self._set_human_status(card, column)  # a human card stays in the column it was typed in
        self.note(f"board: created {card.id} from new card '{card.title}'")
        return card

    def card_line(self, card: Card) -> str:
        box = "x" if card.status == "done" else " "
        who = f" ({card.meta['claimed_by']})" if card.status == "in-progress" and card.meta.get("claimed_by") else ""
        blocked = ""
        if card.status == "backlog" and (b := self.blockers(card)):
            blocked = f" #blocked (waiting on {', '.join(x.split(' ')[0] for x in b)})"
        spike = card.meta.get("type") == "spike" and not card.human
        unmerged = " #not-merged" if card.status == "done" and spike else ""
        human = " #human" if card.human else ""
        return (f"- [{box}] [[{card.id}]] {card.title} #{card.meta.get('type', 'feature')}"
                f"{human}{unmerged}{blocked}{who}")

    def render_board(self) -> None:
        pos = {cid: i for i, cid in enumerate(self.order)}
        cards = self.cards()
        out = ["---", "", "kanban-plugin: board", "", "---", ""]
        for status, name in COLUMNS.items():
            out += [f"## {name}", ""]
            if status == "done":
                out.append("**Complete**")
            lane = [c for c in cards if c.status == status]
            lane.sort(key=lambda c: (pos.get(c.id, len(pos)), c.id))
            out += [self.card_line(c) for c in lane]
            out += ["", ""]
        settings = self.settings or json.dumps({"kanban-plugin": "board"})
        out += ["%% kanban:settings", "```", settings, "```", "%%", ""]
        text = "\n".join(out)
        old = self.board.read_text(encoding="utf-8") if self.board.is_file() else None
        if old != text:
            write_atomic(self.board, text)

    # operations (call through transact)

    def _new(self, kind: str, title: str, description: str, depends=None, human: bool = False,
             publish_to: str | None = None, criteria=None, merge_to: str | None = None,
             agent: str | None = None) -> Card:
        if kind not in TYPES:
            raise MakoError(f"type must be one of {', '.join(TYPES)}")
        if not title.strip():
            raise MakoError("title is required")
        if publish_to and kind != "spike":
            raise MakoError("publish_to is for spikes only")
        merge_to = check_merge_to(self.project, merge_to or "", kind)
        description, lifted = split_criteria(description)
        criteria_text = criteria_lines(criteria) if criteria else lifted or EMPTY_CRITERIA
        cid = self.next_id()
        deps = self.parse_ids(depends)
        self.check_deps(cid, deps)
        meta = {"id": cid, "type": kind, "title": title.strip(), "depends_on": deps,
                "status": "backlog", "created": now(), "reviews": 0}
        if human:
            meta["human"] = "true"
        meta["approved"] = "false" if agent in self.agents else "true"  # worker-made cards need the user's OK
        if merge_to:
            meta["merge_to"] = merge_to
        if kind == "spike":
            meta["publish_to"] = (self.spike_publish_to if publish_to is None else publish_to).strip()
        card = Card(self.open_dir / f"{cid}.md", meta,
                    CARD_BODY.format(title=title.strip(), description=description.strip() or "_TBD_",
                                     criteria=criteria_text))
        self.save(card)
        self.run_hook("new", card)
        return card

    def _claim(self, cid: str, agent: str | None) -> dict:
        if not agent:
            raise MakoError("no agent identity: set MAKO_AGENT or pass --agent <slot>")
        if agent not in self.agents:
            raise MakoError(f"'{agent}' is not an agent slot (slots: {', '.join(self.agents)}); "
                            "only worker slots claim issues")
        card = self.get(cid)
        cid = card.id
        if card.human:
            raise MakoError(f"{cid} is a human task (human: true); agents can't claim it")
        if not card.approved:
            raise MakoError(f"{cid} is not approved: it was created by a worker slot. "
                            "The user approves it by setting `approved: true` in the card's properties.")
        if card.status in ("done", "discarded"):
            raise MakoError(f"{cid} is {'closed' if card.status == 'done' else 'discarded'}; reopen it first")
        lock = self.read_lock(cid)
        if lock and lock.get("agent") == agent:
            return self._claim_info(card, agent)
        if lock:
            raise MakoError(f"{cid} is already claimed by {lock.get('agent')}")
        if blocked := self.blockers(card):
            raise MakoError(f"{cid} is blocked by {', '.join(blocked)}; "
                            "it can be claimed once they are Done")
        held = self.lock_of(agent)
        if held:
            raise MakoError(f"{agent} already holds {held['id']}; close or release it first")
        if self.warn_flag(agent).is_file():
            self.warn_flag(agent).unlink()
            raise MakoError("Unclaimed. Remind the user to run /clear periodically and please try again.")
        wt = self.worktree(agent)
        if not wt.is_dir():
            raise MakoError(f"worktree for {agent} is missing: {wt}")
        if is_broken_worktree(wt):
            raise MakoError(f"{agent}'s worktree is broken: git can't follow its link to the repo. "
                            "This usually means the workspace was last used on another OS. "
                            "Run `mako repair` to re-link it.")
        if not is_clean(wt):
            raise MakoError(f"{agent}'s worktree has uncommitted changes; commit or stash them first")
        branch = card.meta.get("branch") or f"{card.meta.get('type', 'feature')}/{cid}-{slugify(card.title)}"
        lock_dir = self.locks / cid
        try:
            lock_dir.mkdir()
        except FileExistsError:
            raise MakoError(f"{cid} was just claimed by someone else") from None
        try:
            if branch_exists(self.project, branch):
                git(wt, "switch", branch)
            else:
                git(wt, "switch", "-c", branch, self.base)
        except MakoError as e:
            shutil.rmtree(lock_dir, ignore_errors=True)
            raise MakoError(f"could not check out {branch} in {agent}'s worktree: {e}") from None
        owner = {"id": cid, "agent": agent, "branch": branch, "worktree": str(wt), "claimed": now()}
        (lock_dir / "owner.json").write_text(json.dumps(owner, indent=2), encoding="utf-8")
        card.meta.update(status="in-progress", branch=branch, claimed_by=agent, claimed=now())
        if card.meta.get("type") == "spike":
            card.meta["summary"] = self.summary_path(card)
            try:
                self._start_summary(card, wt, agent)
            except MakoError as e:
                _git(wt, "reset", "-q", "--hard")
                _git(wt, "switch", "--detach", self.base)
                shutil.rmtree(lock_dir, ignore_errors=True)
                raise MakoError(f"could not write the spike summary in {agent}'s worktree: {e}") from None
        self.save(card)
        self.run_hook("claim", card, owner)
        return self._claim_info(card, agent)

    def _start_summary(self, card: Card, wt: Path, agent: str) -> None:
        """Commit the spike's summary file, frontmatter filled in, onto its branch in the
        slot's worktree, so the worktree stays clean. A resumed spike keeps the one it has."""
        path = card.meta["summary"]
        if (wt / path).is_file():
            return
        meta = {"id": card.id, "title": card.title, "agent": agent, "created": now(),
                "branch": card.meta["branch"], "closed": ""}
        lines = ["---", *(f"{k}: {_yaml_value(meta[k])}" for k in SUMMARY_FIELDS), "---"]
        text = "\n".join(lines) + "\n" + SUMMARY_BODY.format(title=card.title)
        commit_file(wt, path, text.encode("utf-8"), f"{card.id}: spike summary")

    def _claim_info(self, card: Card, agent: str) -> dict:
        info = {"id": card.id, "title": card.title, "type": card.meta.get("type"),
                "branch": card.meta.get("branch"), "worktree": str(self.worktree(agent)),
                "card": str(card.path)}
        if card.meta.get("type") == "spike":
            info["summary"] = str(self.worktree(agent) / self.summary_path(card))
        return info

    def _check_owner(self, cid: str, lock: dict, agent: str | None, force: bool) -> None:
        # Humans (no identity) and the orchestrator may act on any claim; a worker
        # slot may only act on its own.
        if agent in self.agents and agent != lock.get("agent") and not force:
            raise MakoError(f"{cid} is claimed by {lock.get('agent')}, not {agent}")

    def _free_slot(self, cid: str, lock: dict, branch: str) -> None:
        """Put the owning slot back on a detached base branch and drop the lock."""
        wt = self.worktree(lock.get("agent", ""))
        if wt.is_dir() and current_branch(wt) == branch:
            if not is_clean(wt):
                raise MakoError(f"{lock.get('agent')}'s worktree has uncommitted changes on {branch}; "
                                "commit or discard them first")
            git(wt, "switch", "--detach", self.base)
        shutil.rmtree(self.locks / cid, ignore_errors=True)

    def _append(self, card: Card, heading: str, agent: str | None, note: str) -> None:
        if note.strip():
            card.body = card.body.rstrip("\n") + f"\n\n## {heading} ({agent or 'human'}, {now()})\n\n{note.strip()}\n"

    def _close_refusals(self, cid: str, agent: str | None, force: bool) -> tuple[Card, dict | None, Path | None]:
        """The checks every close starts with: the card is open, the caller may close it and
        the owning slot's worktree is clean. Returns the card, its lock and that worktree."""
        card = self.get(cid)
        branch = card.meta.get("branch", "")
        if card.status == "done":
            raise MakoError(f"{card.id} is already closed")
        if card.status == "discarded":
            raise MakoError(f"{card.id} is discarded; `mako reopen {card.id}` it first")
        lock = self.read_lock(card.id)
        wt = self.worktree(lock.get("agent", "")) if lock else None
        if lock:
            self._check_owner(card.id, lock, agent, force)
            if wt.is_dir() and current_branch(wt) == branch and not is_clean(wt):
                raise MakoError(f"{lock.get('agent')}'s worktree has uncommitted changes on {branch}; "
                                "commit them first")
        return card, lock, wt

    def _review_gate(self, lock: dict | None, head: str, force: bool, gate: bool) -> None:
        if gate and self.reviewing and lock and not force and lock.get("reviewed") != head:
            raise MakoError(f"call `request_review` first (no review for {head[:7]})")

    def _pre_close_target(self, cid: str, agent: str | None, force: bool,
                          gate: bool) -> tuple[Card, dict | None, str, Path | None] | None:
        """What run_pre_close checks: (card, lock, the branch's HEAD, the slot's worktree if it is
        on the branch), after every refusal a close would make first. None: nothing to check
        (a spike, or no commits to merge)."""
        card, lock, wt = self._close_refusals(cid, agent, force)
        branch = card.meta.get("branch", "")
        if card.meta.get("type") == "spike" or not unmerged_commits(self.project, branch, self.base):
            return None
        head = git(self.project, "rev-parse", branch)
        self._review_gate(lock, head, force, gate)
        self._check_behind(card.id, branch)  # before a slow check, not after it
        return card, lock, head, wt if wt and wt.is_dir() and current_branch(wt) == branch else None

    def _close(self, cid: str, agent: str | None, note: str, force: bool, gate: bool = False,
               checked: str = "", unchecked: bool = False) -> Card:
        """Finish an issue. If its branch has commits they are merged into the
        base branch first; the branch must already contain the base branch, so
        conflicts are resolved by the worker in its own worktree, never here.
        With gate (the agents' close_issue) and a review step (the default), the
        branch's HEAD must be the commit the last request_review covered.
        With a pre_close_hook, HEAD must be the commit it passed on (checked),
        unless unchecked (`mako close --force` by a human).
        Spikes never merge: their result is the closing note, and a branch with
        commits is kept as it is (no review gate, no behind-base check)."""
        card, lock, wt = self._close_refusals(cid, agent, force)
        branch = card.meta.get("branch", "")
        spike = card.meta.get("type") == "spike"
        published = None
        if spike:
            published = self._finish_summary(card, wt if wt and current_branch(wt) == branch else None, force)
        pending = unmerged_commits(self.project, branch, self.base)
        head = git(self.project, "rev-parse", branch) if branch and branch_exists(self.project, branch) else ""
        if pending and not spike:
            self._review_gate(lock, head, force, gate)
        if self.hooks["pre_close"] and pending and not spike:
            if unchecked:
                self.note(f"warning: --force: the pre_close_hook was not run for {card.id}")
            elif head != checked:
                raise MakoError(f"{branch} changed while the pre_close_hook ran; close {card.id} again")
        if pending and not spike:
            self._merge_into_base(card.id, branch, card.meta.get("merge_to") or "")
        if lock:
            self._free_slot(card.id, lock, branch)
        # A branch that never had commits is always deleted: there is nothing to keep.
        # With keep_branches, one that was merged (now or before a reopen) is kept for `mako prune`.
        keep = pending if spike else self.keep_branches and (pending or card.meta.get("merged"))
        if branch and branch_exists(self.project, branch) and not keep:
            # Fully merged (or never had commits). -d only knows the base branch, which a
            # merge_to merge didn't touch; _merge_into_base already did the merge.
            git(self.project, "branch", "-D" if card.meta.get("merge_to") and pending else "-d", branch)
        if pending and not spike:
            card.meta["merged"] = now()
        if spike:
            kept = f"; branch kept: {branch}" if pending else ""
            where = f" Summary published to {published}." if published else ""
            if published:
                self.note(f"published {card.id}'s summary to {published} (not committed)")
            note = f"Spike closed without merging{kept}.{where}\n\n{note.strip()}"
        card.meta.update(status="done", closed=now())
        self._append(card, "Closing note", agent, note)
        self.save(card)
        merged = pending and not spike
        self._review_finish(card, lock, "close", f"merged into {card.meta.get('merge_to') or self.base}"
                            if merged else "closed without merging", note)
        if lock:
            self._set_warn_flag(card, lock.get("agent", ""))
        self.run_hook("close", card, lock, {"sha": head})
        return card

    def _finish_summary(self, card: Card, wt: Path | None, force: bool) -> Path | None:
        """On closing a spike: fill in the summary's `closed` (committed in the slot's
        worktree, when it is on the branch) and copy it to publish_to, if set. A plain
        copy, never committed; a re-close overwrites it. With publish_to set, a missing
        summary is refused unless forced."""
        branch, path = card.meta.get("branch", ""), self.summary_path(card)
        target = self.publish_target(card)
        data = git_file(self.project, branch, path) if branch and branch_exists(self.project, branch) else None
        if data is None:
            if target and not force:
                raise MakoError(f"{card.id} has no summary {path} on {branch or 'its branch'} to publish "
                                f"to {card.meta['publish_to']}; write and commit it, or close with --force")
            if target:
                self.note(f"warning: {card.id} has no summary {path}; nothing was published")
            return None
        text = data.decode("utf-8").replace("\r\n", "\n")
        filled = re.sub(r"\A(---\n(?:(?!---\n).*\n)*?)closed:[^\n]*\n", lambda mm: f"{mm.group(1)}closed: "
                        f"{_yaml_value(now())}\n", text, count=1)
        if wt and filled != text:
            commit_file(wt, path, filled.encode("utf-8"), f"{card.id}: spike summary closed")
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(filled.encode("utf-8"))
        return target

    def _check_behind(self, cid: str, branch: str) -> None:
        if not git_ok(self.project, "merge-base", "--is-ancestor", self.base, branch):
            raise MakoError(f"{branch} is behind {self.base}. In your worktree run `git merge {self.base}`, "
                            f"resolve any conflicts, run the tests and commit, then close {cid} again")

    def _merge_into_base(self, cid: str, branch: str, merge_to: str = "") -> None:
        """Merge the issue branch into merge_to (created from the base branch if missing),
        else into the base branch. The main checkout is left on the base branch."""
        target = merge_to or self.base
        self._check_behind(cid, branch)
        if current_branch(self.project) != self.base:
            raise MakoError(f"the main checkout {self.project} must be on {self.base} to merge")
        if not is_clean(self.project):
            raise MakoError(f"the main checkout {self.project} has uncommitted changes; "
                            "it must be clean to merge")
        created = False
        if target != self.base:
            if not branch_exists(self.project, target):
                git(self.project, "branch", target, self.base)
                created = True
            try:
                git(self.project, "switch", target)
            except MakoError as e:
                if created:
                    git(self.project, "branch", "-D", target)
                raise MakoError(f"could not switch to {target} to merge {cid}: {e}") from None
        try:
            title = self.get(cid).title
            message = f"Merge {branch} ({cid}): {title}" if self.keep_branches else f"Merge {cid}: {title}"
            git(self.project, "merge", "--no-ff", "-m", message, branch)
        except MakoError as e:
            _git(self.project, "merge", "--abort")
            _git(self.project, "switch", self.base)
            if created:
                _git(self.project, "branch", "-D", target)
            raise MakoError(f"merging {branch} into {target} failed, nothing was changed: {e}") from None
        finally:
            if target != self.base and current_branch(self.project) != self.base:
                git(self.project, "switch", self.base)

    def _release(self, cid: str, agent: str | None, note: str, force: bool) -> Card:
        """Back to the Backlog without finishing. The branch is kept, so the
        next claim resumes it."""
        card = self.get(cid)
        if card.status in ("done", "discarded"):
            raise MakoError(f"{card.id} is {'closed' if card.status == 'done' else 'discarded'}")
        lock = self.read_lock(card.id)
        if lock:
            self._check_owner(card.id, lock, agent, force)
            self._free_slot(card.id, lock, card.meta.get("branch", ""))
        card.meta.update(status="backlog", claimed_by="", claimed="")
        self._append(card, "Released", agent, note)
        self.save(card)
        self._review_finish(card, lock, "release", "released, back in the Backlog", note)
        return card

    def _review(self, cid: str, agent: str | None) -> dict:
        """Call the review backend's request hook (then run review_hook, if set), or without a
        backend launch review_hook as the review step, or with neither (the manual step) return
        the diff stat the agent shows the user; then record the commit it covers on the claim
        (cleared with the claim on close/release)."""
        if not self.reviewing:
            raise MakoError('no review step: auto_approve is on in .mako/config.json and neither '
                            '"review_hook" nor "review" is set')
        if self.auto_approve:
            self.note("note: auto_approve is ignored while a review step is set (review_hook or review)")
        card = self.get(cid)
        cid = card.id
        lock = self.read_lock(cid)
        if card.status != "in-progress" or not lock:
            raise MakoError(f"{cid} is not in progress; only a claimed issue can be reviewed")
        self._check_owner(cid, lock, agent, force=False)
        branch = card.meta.get("branch", "")
        wt = self.worktree(lock.get("agent", ""))
        if not branch_exists(self.project, branch) or not wt.is_dir():
            raise MakoError(f"{cid} has no branch or worktree to review")
        if not is_clean(wt):
            raise MakoError(f"{lock.get('agent')}'s worktree has uncommitted changes; "
                            "commit them first, the review covers commits")
        head = git(self.project, "rev-parse", branch)
        if self.review_backend:
            issue = {"id": cid, "title": card.title, "branch": branch, "agent": lock.get("agent", ""), "sha": head}
            try:
                with self.backend_errors() as hooks:
                    sent = hooks.request(self.review_context(), issue) or {}
            except MakoError as e:
                raise MakoError(f"{e}. No review was recorded; call request_review again once that is fixed") from None
            if sent.get("ref") is not None:
                lock["review_ref"] = sent["ref"]
            lock["review_revision"] = sent.get("revision")
            info = {"text": str(sent.get("text") or f"Sent {cid} for review.")}
            self.run_hook("review", card, lock, {"sha": head, "review_ref": sent.get("ref"),
                                                 "review_url": sent.get("url")})
        elif self.manual_review:
            stat = git(self.project, "diff", "--stat", f"{self.base}...{branch}")
            info = {"manual": True, "branch": branch, "base": self.base, "stat": stat}
        else:
            log, commands = self.run_hook("review", card, lock, {"sha": head}, strict=True)
            info = {"log": str(log), "command": "\n  ".join(commands)}
        lock.update(reviewed=head, review_requested=now())
        (self.locks / cid / "owner.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
        # Only reached once the tool has launched. Every round counts, a re-run on the same commit too.
        card.meta["reviews"] = review_count(card) + 1
        self.save(card)
        return {"id": cid, "sha": head, **info}

    # hooks (described at HOOK_EVENTS)

    def run_hook(self, event: str, card: Card, lock: dict | None = None, extra: dict | None = None,
                 strict: bool = False) -> tuple[Path | None, list[str]]:
        """Launch the event's hook commands, detached and in order, with their parameters as
        placeholders and environment variables; logged to .mako/logs/<ID>.<event>.log. A launch
        failure only warns, unless strict (review_hook as the review step). Returns the log and
        the expanded commands."""
        if not self.hooks.get(event):
            return None, []
        expanded, env, values = self.hook_setup(event, card, lock, extra)
        sha, agent = values["sha"], values["agent"]
        cwd = {"new": self.root, "close": self.project}.get(event) or self.worktree(agent)
        log = self.dir / "logs" / f"{card.id}.{event}.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8") as out:
                for cmd in expanded:
                    out.write(f"# {now()}  {card.id}{f' at {sha}' if sha else ''}\n# cwd: {cwd}\n$ {cmd}\n")
                    out.flush()
                    try:
                        spawn_detached(cmd, cwd, out, env)
                    except OSError as e:
                        out.write(f"# could not launch: {e}\n")
                        raise
        except OSError as e:
            if strict:
                raise MakoError(f"could not launch the {event}_hook: {e}") from None
            self.note(f"warning: could not launch the {event}_hook for {card.id}: {e} (log: {log})")
        return log, expanded

    def hook_setup(self, event: str, card: Card, lock: dict | None,
                   extra: dict | None) -> tuple[list[str], dict, dict]:
        """The event's commands with their placeholders expanded, their environment, and the
        built-in parameter values."""
        commands = self.hooks.get(event) or []
        extra = {k: "" if v is None else str(v) for k, v in (extra or {}).items()}
        agent = (lock or {}).get("agent", "")
        branch = card.meta.get("branch", "")
        sha = extra.get("sha")
        if sha is None:
            sha = git(self.project, "rev-parse", branch) if branch and branch_exists(self.project, branch) else ""
        values = {"id": card.id, "title": card.title, "type": card.meta.get("type", ""), "base": self.base,
                  "branch": branch, "sha": sha, "agent": agent, "root": str(self.root),
                  "repo": str(self.project), "worktree": str(self.worktree(agent)) if agent else "",
                  "review_ref": "", "review_url": ""}
        values.update((k, v) for k, v in extra.items() if k in values)
        unknown: list[str] = []
        user = {}
        for name, raw in self.hook_params.items():
            if name in HOOK_PARAMS:
                self.note(f"warning: hook_params can't override the built-in parameter {{{name}}}; ignored")
            elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                self.note(f"warning: hook_params name '{name}' is not a valid parameter name; ignored")
            else:  # built-in placeholders in the value are expanded, once
                user[name] = expand_placeholders("" if raw is None else str(raw), values, unknown)
        # Never MAKO_ROOT: a hook that runs `mako` would act on that workspace. MAKO_WORKSPACE instead.
        env = {k: v for k, v in os.environ.items() if k.upper() != "MAKO_ROOT"}
        env.update({f"MAKO_{k.upper()}": v for k, v in values.items() if k != "root"})
        env.update({f"MAKO_PARAM_{k.upper()}": v for k, v in user.items()})
        env.update(MAKO_WORKSPACE=values["root"], MAKO_EVENT=event)
        expanded = [expand_placeholders(cmd, {**values, **user}, unknown) for cmd in commands]
        if unknown:
            names = ", ".join("{" + name + "}" for name in unknown)
            self.note(f"warning: {event}_hook: unknown parameter {names} left as it is")
        return expanded, env, values

    def run_pre_close(self, card: Card, lock: dict | None, head: str, wt: Path | None) -> None:
        """Run the pre_close_hook commands in order and wait for each, in wt (the slot's
        worktree, on the branch) or, without one, a temporary worktree at head. The first
        that fails or times out refuses the close. Logged to .mako/logs/<ID>.pre_close.log."""
        temp = None
        if wt is None:
            temp = self.dir / "tmp" / f"pre_close-{card.id}"
            self._drop_temp_worktree(temp)
            temp.parent.mkdir(parents=True, exist_ok=True)
            git(self.project, "worktree", "add", "--detach", str(temp), head)
            wt = temp
        try:
            expanded, env, _ = self.hook_setup("pre_close", card, lock, {"sha": head, "worktree": wt})
            log = self.dir / "logs" / f"{card.id}.pre_close.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("", encoding="utf-8")
            timeout = self.pre_close_timeout or None
            for cmd in expanded:
                with open(log, "a", encoding="utf-8") as out:
                    out.write(f"# {now()}  {card.id} at {head}\n# cwd: {wt}\n$ {cmd}\n")
                    out.flush()
                    start = log.stat().st_size
                    try:
                        code = run_and_wait(cmd, wt, out, env, timeout)
                    except OSError as e:
                        out.write(f"# could not launch: {e}\n")
                        raise MakoError(f"pre_close_hook: could not launch `{cmd}`: {e}; {card.id} "
                                        f"was not closed (log: {log})") from None
                    out.write(f"# timed out after {self.pre_close_timeout}s, killed\n" if code is None
                              else f"# exit code {code}\n")
                if code != 0:
                    with open(log, "rb") as f:
                        f.seek(start)
                        lines = f.read().decode("utf-8", "replace").replace("\r\n", "\n").splitlines()
                    tail = "\n".join(lines[-21:-1])  # the last line is our "# exit code"/"# timed out"
                    why = (f"timed out after {self.pre_close_timeout}s (killed)" if code is None
                           else f"failed with exit code {code}")
                    raise MakoError(f"pre_close_hook: `{cmd}` {why}; {card.id} was not closed. Fix it on "
                                    f"the branch, commit and close again. Log: {log}"
                                    + (f"\n--- last lines of its output ---\n{tail}" if tail.strip() else ""))
            if not temp and not is_clean(wt):
                raise MakoError(f"the pre_close_hook passed but left changes in {wt} (see `git status`); "
                                f"{card.id} was not closed. Gitignore its output, or have it clean up, "
                                "then close again")
        finally:
            if temp:
                self._drop_temp_worktree(temp)

    def _drop_temp_worktree(self, path: Path) -> None:
        if path.exists():
            _git(self.project, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
        _git(self.project, "worktree", "prune")

    # review backend (the hooks are described at load_review_backend)

    def backend(self):
        if not re.fullmatch(r"[a-z0-9_-]+", self.review_backend):
            raise MakoError(f"bad review backend name '{self.review_backend}' in .mako/config.json")
        return load_review_backend(self.dir / "templates" / "review" / f"{self.review_backend}.py")

    def review_context(self) -> dict:
        return {"root": self.root, "repo": self.project, "project": self.config["project"], "base": self.base,
                "agents": list(self.agents), "options": self.review_options,
                "state_dir": self.dir / "review" / self.review_backend,
                "workspace_tag": self.config.get("session_tag") or self.prefix.lower()}

    @contextmanager
    def backend_errors(self):
        """Yields the backend's hooks; its ReviewError becomes a MakoError with the same message."""
        hooks = self.backend()
        try:
            yield hooks
        except hooks.ReviewError as e:
            raise MakoError(str(e)) from None

    def review_setup(self) -> str:
        if self.backend_hook("setup") is None:
            raise MakoError('no review backend with a setup step ("review" in .mako/config.json)')
        with self.backend_errors() as hooks:
            return hooks.setup(self.review_context())

    def _review_finish(self, card: Card, lock: dict | None, event: str, outcome: str, note: str) -> None:
        """After close, release or discard of an issue the backend was sent: tell it, and on close
        keep the transcript it returns in review_transcripts_to, if set. Never fails the command: a
        backend that is down costs a warning."""
        if not self.review_backend or not lock or lock.get("review_ref") is None:
            return
        issue = {"id": card.id, "title": card.title, "branch": card.meta.get("branch", ""),
                 "agent": lock.get("agent", ""), "ref": lock["review_ref"]}
        try:
            if self.backend_hook("finish") is None:
                return
            with self.backend_errors() as hooks:
                result = hooks.finish(self.review_context(), issue, event, outcome, note) or {}
        except MakoError as e:
            self.note(f"warning: {e}. The review of {card.id} was left as it is")
            return
        if event == "close" and result.get("transcript") and self.review_transcripts_to:
            folder = Path(self.review_transcripts_to)
            target = (folder if folder.is_absolute() else self.root / folder) / f"review_{card.id}.md"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(target, result["transcript"])
            except OSError as e:
                self.note(f"warning: could not write the review transcript to {target}: {e}")
                return
            self.note(f"wrote the review transcript to {target}")

    def review_status(self, cid: str) -> dict:
        """The backend's report on the issue's review."""
        if not self.review_has_status:
            raise MakoError("review_status needs a review backend that reports status "
                            '("review" in .mako/config.json)')
        card = self.get(cid)
        with self.backend_errors() as hooks:
            report = hooks.status(self.review_context(), card.id)
        # Mako's own fields, so a reviewing slot can read the uploaded change from its worktree.
        lock = self.read_lock(card.id)
        if isinstance(report, dict) and lock and lock.get("reviewed"):
            sha = lock["reviewed"]
            report.update(sha=sha, parent=self._merge_base(sha), author=lock.get("agent"))
        return report

    def waiting_for_review(self, card: Card) -> bool:
        """Claimed, sent for review, and no commits since: what a reviewer would look at."""
        lock = self.read_lock(card.id)
        branch = card.meta.get("branch", "")
        if not lock or not lock.get("reviewed") or not branch:
            return False
        head = _git(self.project, "rev-parse", "--verify", "--quiet", branch)
        return head.returncode == 0 and head.stdout.strip() == lock["reviewed"]

    def _merge_base(self, sha: str) -> str:
        return git(self.project, "merge-base", self.base, sha)

    def _post_review(self, cid: str, reviewer: str | None, sha: str | None, message: str, score: int,
                     comments: list | None, files: list | None) -> dict:
        """Post a review of another slot's uploaded change to the backend's comment hook, as
        reviewer: a worker slot, or `human` (the CLI without MAKO_AGENT). Refused unless it is of
        the revision the last request_review uploaded (sha), and every comment is on a file the
        change modifies, at a line that exists at sha. Advisory: the close gate never reads it."""
        if not self.review_has_comment:
            raise MakoError("posting reviews needs a review backend that takes comments "
                            '("review" in .mako/config.json)')
        if reviewer != "human" and reviewer not in self.agents:
            raise MakoError(f"only worker slots ({', '.join(self.agents)}) can post reviews; "
                            f"{reviewer or 'the orchestrator'} is not one")
        card = self.get(cid)
        cid = card.id
        lock = self.read_lock(cid)
        if card.status != "in-progress" or not lock:
            raise MakoError(f"{cid} is not in progress; only a claimed issue in review can be reviewed")
        if not lock.get("reviewed") or lock.get("review_ref") is None:
            raise MakoError(f"{cid} has no review to post to: {lock.get('agent')} hasn't called request_review")
        author = lock.get("agent")
        if reviewer == author:
            raise MakoError(f"{cid} is your own claim; you can't review it. Another slot or the user reviews it")
        current = lock["reviewed"]
        if sha is not None:
            given = str(sha).strip()
            resolved = _git(self.project, "rev-parse", "--verify", "--quiet", f"{given}^{{commit}}") if given else None
            if not resolved or resolved.returncode or resolved.stdout.strip() != current:
                raise MakoError(f"{cid} has a newer upload ({current[:7]}) than {given[:7] or 'the sha you gave'}; "
                                "call review_status again and review that")
        limit = 2 if reviewer == "human" else 1
        try:
            score = int(score)
        except (TypeError, ValueError):
            raise MakoError(f"score must be a whole number from -{limit} to {limit}") from None
        if not -limit <= score <= limit:
            who = "the user" if reviewer == "human" else "agents"
            raise MakoError(f"score {score} is out of range: {who} score -{limit} to {limit}"
                            + ("" if reviewer == "human" else " (only the user approves)"))
        parent = self._merge_base(current)
        changed = git(self.project, "-c", "core.quotePath=false", "diff", "--name-only", parent, current).splitlines()
        scope = [self._review_path(p) for p in files or []]
        for path in scope:
            if path not in changed:
                raise MakoError(f"{path} is not changed by {cid}; it changes: {', '.join(changed)}")
        if scope and score > 0:
            raise MakoError(f"a review of only some files can't score +{score}: they can't vouch for the "
                            "whole change. Score 0 or -1, or review all of it")
        found = []
        for raw in comments or []:
            if not isinstance(raw, dict) or not str(raw.get("message") or "").strip() or not raw.get("path"):
                raise MakoError(f"each comment needs a path and a message: {raw!r}")
            path, line = self._review_path(raw["path"]), raw.get("line")
            if path not in changed:
                raise MakoError(f"{path} is not changed by {cid}, so it can't take a comment; "
                                f"it changes: {', '.join(changed)}")
            if scope and path not in scope:
                raise MakoError(f"{path} is outside the files you reviewed ({', '.join(scope)})")
            if line is not None:
                data = git_file(self.project, current, path)
                lines = 0 if data is None else data.count(b"\n") + (not data.endswith(b"\n") and bool(data))
                if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                    raise MakoError(f"bad line {line!r} for {path}: a line number from 1, or none for the file")
                if line > lines:
                    raise MakoError(f"{path} has {lines} line{'s' * (lines != 1)} at {current[:7]}, "
                                    f"so there is no line {line}"
                                    + (" (it is deleted there: comment on the file)" if data is None else ""))
            found.append({"path": path, "line": line, "message": str(raw["message"]).strip()})
        message = str(message or "").strip()
        if scope:
            message = f"Reviewed only: {', '.join(scope)}" + (f"\n\n{message}" if message else "")
        if not message and not found and not score:
            raise MakoError("nothing to post: give a message, comments or a score")
        issue = {"id": cid, "title": card.title, "branch": card.meta.get("branch", ""), "agent": author,
                 "sha": current, "ref": lock["review_ref"], "revision": lock.get("review_revision")}
        review = {"reviewer": reviewer, "message": message, "score": score, "comments": found}
        with self.backend_errors() as hooks:
            posted = hooks.comment(self.review_context(), issue, review) or {}
        return {"id": cid, "author": author, "sha": current, "url": posted.get("url"),
                "text": str(posted.get("text") or f"Posted your review of {cid}.")}

    @staticmethod
    def _review_path(path) -> str:
        """A repo path as git prints it: forward slashes, no leading ./"""
        text = str(path).strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        return text

    def _discard(self, cid: str, agent: str | None, note: str, force: bool) -> Card:
        """Drop an issue without merging: the card moves to kanban/discarded/ and
        off the board. A claim is freed, but its branch is kept, so reopening and
        claiming again resumes it. Uncommitted work in the slot's worktree is
        refused; force commits it onto the kept branch instead, so nothing is lost."""
        card = self.get(cid)
        cid = card.id
        if card.status == "done":
            merged = " and merged" if card.meta.get("merged") else ""
            raise MakoError(f"{cid} is closed{merged}; it can't be discarded")
        if card.status == "discarded":
            raise MakoError(f"{cid} is already discarded")
        branch = card.meta.get("branch", "")
        lock = self.read_lock(cid)
        owner = lock.get("agent") if lock else card.meta.get("claimed_by") or None
        if lock:
            self._check_owner(cid, lock, agent, force)
        elif owner and agent in self.agents and agent != owner and not force:
            raise MakoError(f"{cid} is claimed by {owner}, not {agent}")
        if lock:
            wt = self.worktree(owner or "")
            if wt.is_dir() and current_branch(wt) == branch and not is_clean(wt):
                if not force:
                    raise MakoError(f"{owner}'s worktree has uncommitted changes on {branch}; commit them "
                                    "first, or use --force to commit them onto the kept branch")
                git(wt, "add", "-A")
                git(wt, "commit", "-q", "-m", f"{cid}: uncommitted work saved when the issue was discarded")
                self.note(f"committed {owner}'s uncommitted changes to {branch}")
            self._free_slot(cid, lock, branch)
        was = card.status
        card.meta.update(status="discarded", discarded=now(), claimed_by="", claimed="")
        self._append(card, "Discarded", agent, note)
        self.save(card)
        self._review_finish(card, lock, "discard", "discarded, not merged", note)
        if branch and branch_exists(self.project, branch):
            head = (f"{cid} was in progress ({owner})." if was == "in-progress"
                    else f"{cid} still had a branch from an earlier claim.")
            self.note(f"warning: {head} Its branch {branch} was kept and nothing was merged. "
                      f"Delete it with `mako prune --discarded {cid}` if the work isn't needed.")
        for c in self.dependents(cid):
            if c.status in ("backlog", "in-progress"):
                self.note(f"warning: {c.id} depends on {cid} and is now blocked; "
                          f"run `mako undepend {c.id} {cid}`")
        return card

    def _reopen(self, cid: str, note: str = "") -> Card:
        card = self.get(cid)
        if card.status not in ("done", "discarded"):
            raise MakoError(f"{card.id} is not closed or discarded")
        was = card.status
        shutil.rmtree(self.locks / card.id, ignore_errors=True)
        card.meta.update(status="backlog", claimed_by="", claimed="", closed="", discarded="")
        self._append(card, "Reopened", None, note)
        self.save(card)
        branch = card.meta.get("branch", "")
        if was == "discarded" and branch and branch_exists(self.project, branch):
            self.note(f"{card.id}'s branch {branch} was kept; the next claim resumes it")
        started = [f"{c.id} ({COLUMNS.get(c.status, c.status)})" for c in self.dependents(card.id)
                   if c.status != "backlog"]
        if started:
            self.note(f"warning: issues that depend on {card.id} have already started: "
                      f"{', '.join(started)}; nothing was undone")
        return card

    def _edit(self, cid: str, title: str | None, kind: str | None,
              description: str | None, append: str | None,
              depends=None, add_deps=None, remove_deps=None, human=None, publish_to=None,
              criteria=None, merge_to=None) -> Card:
        """Change a Backlog card's title, type, body, dependencies, human marker, merge_to or
        (spikes) publish_to. Holds the issue's claim lock while checking and writing, so a claim and
        an edit can't overlap. publish_to alone may also change while the spike is In Progress."""
        card = self.get(cid)
        cid = card.id
        if card.status in ("done", "discarded"):
            state = "closed" if card.status == "done" else "discarded"
            raise MakoError(f"{cid} is {state}; `mako reopen {cid}` it first")
        if kind is not None and kind not in TYPES:
            raise MakoError(f"type must be one of {', '.join(TYPES)}")
        if publish_to is not None:
            if (kind or card.meta.get("type")) != "spike":
                raise MakoError("publish_to is for spikes only")
            others = (title, kind, description, append, depends, add_deps, remove_deps, human, criteria,
                      merge_to)
            if card.status == "in-progress" and all(v is None for v in others):
                card.meta["publish_to"] = publish_to.strip()
                self.save(card)
                return card
        if merge_to is not None:
            merge_to = check_merge_to(self.project, merge_to, kind or card.meta.get("type"))
        if title is not None and not title.strip():
            raise MakoError("title can't be empty")
        lifted = None
        if description is not None:
            description, lifted = split_criteria(description)
        if criteria is not None:  # an explicit list wins over criteria lifted from the description
            lifted = criteria_lines(criteria) if criteria else EMPTY_CRITERIA
        lock_dir = self.locks / cid
        try:
            lock_dir.mkdir()
        except FileExistsError:
            who = (self.read_lock(cid) or {}).get("agent", "?")
            raise MakoError(f"{cid} is in progress ({who}); release it first "
                            "or leave a note for the worker") from None
        try:
            card = read_card(card.path)
            if card.status != "backlog" or card.meta.get("claimed_by"):
                who = card.meta.get("claimed_by") or "?"
                raise MakoError(f"{cid} is in progress ({who}); release it first "
                                "or leave a note for the worker")
            if title is not None:
                old = card.title
                card.meta["title"] = title.strip()
                heading = re.compile(rf"(?m)^# {re.escape(old)}[ \t]*$")
                if not heading.search(card.body):  # hand-edited: take the first `# ` line
                    heading = re.compile(r"(?m)^# .*$")
                card.body = heading.sub(lambda _: f"# {title.strip()}", card.body, count=1)
            if kind is not None:
                card.meta["type"] = kind
                if kind == "spike":
                    card.meta.setdefault("publish_to", self.spike_publish_to)
                    card.meta.pop("merge_to", None)
                else:
                    card.meta.pop("publish_to", None)
            if description is not None:
                card.body = set_section(card.body, cid, "Description", description.strip() or "_TBD_")
            if lifted is not None:
                card.body = set_section(card.body, cid, "Acceptance criteria", lifted)
            if append is not None and append.strip():
                card.body = card.body.rstrip("\n") + f"\n\n{append.strip()}\n"
            if depends is not None or add_deps or remove_deps:
                deps = self.parse_ids(depends) if depends is not None else self.deps(card)
                deps += [d for d in self.parse_ids(add_deps) if d not in deps]
                gone = self.parse_ids(remove_deps)
                deps = [d for d in deps if d not in gone]
                self.check_deps(cid, deps)
                card.meta["depends_on"] = deps
            if human is not None:
                if human:
                    card.meta["human"] = "true"
                else:
                    card.meta.pop("human", None)
            if publish_to is not None:
                card.meta["publish_to"] = publish_to.strip()
            if merge_to is not None:
                if merge_to:
                    card.meta["merge_to"] = merge_to
                else:
                    card.meta.pop("merge_to", None)
            self.save(card)
        finally:
            shutil.rmtree(lock_dir, ignore_errors=True)
        return card

    def _prune(self, ids, dry_run: bool, discarded: bool) -> list[str]:
        """Delete the local branches of Done cards (`git branch -d`, merged ones only) and,
        with discarded, of discarded cards (`git branch -D`: they are unmerged by definition).
        Never a Backlog or In Progress card's branch, the base branch or one checked out
        in a worktree. With ids, only those issues, and each one is reported."""
        wanted = [self.get(cid).id for cid in self.parse_ids(ids)]
        porcelain = git(self.project, "worktree", "list", "--porcelain").splitlines()
        checked_out = {line[len("branch refs/heads/"):] for line in porcelain
                       if line.startswith("branch refs/heads/")}
        lines = []
        for card in self.cards():
            if wanted and card.id not in wanted:
                continue
            branch = card.meta.get("branch", "")
            label = f"{branch} ({card.id})"
            if card.status not in ("done", "discarded"):
                if wanted:
                    lines.append(f"skipped {card.id}: {COLUMNS[card.status]} issues keep their branch")
                continue
            if not branch or not branch_exists(self.project, branch):
                if wanted:
                    lines.append(f"skipped {card.id}: it has no branch")
                continue
            if card.status == "discarded" and not discarded:
                if wanted:
                    lines.append(f"skipped {label}: discarded; add --discarded to delete it")
                continue
            if branch == self.base or branch in checked_out:
                lines.append(f"skipped {label}: checked out in a worktree")
                continue
            target = card.meta.get("merge_to") or self.base
            if not branch_exists(self.project, target):
                target = self.base
            unmerged = unmerged_commits(self.project, branch, target)
            lost = f", losing {unmerged} unmerged commit{'s' * (unmerged != 1)}" if unmerged else ""
            if card.status == "done" and unmerged:
                spike = " (a spike's branch)" if card.meta.get("type") == "spike" else ""
                lines.append(f"skipped {label}: not fully merged, {unmerged} commit"
                             f"{'s' * (unmerged != 1)} not in {target}{spike}")
                continue
            if dry_run:
                lines.append(f"would delete {label}{lost}")
                continue
            try:
                flag = "-D" if card.status == "discarded" or target != self.base else "-d"
                git(self.project, "branch", flag, branch)
            except MakoError as e:
                lines.append(f"skipped {label}: {e}")
                continue
            lines.append(f"deleted {label}{lost}")
        return lines

    def _next_backlog(self, kind: str | None) -> Card:
        pos = {cid: i for i, cid in enumerate(self.order)}
        backlog = [c for c in self.cards()
                   if c.status == "backlog" and not self.read_lock(c.id) and not c.human and c.approved
                   and (kind is None or c.meta.get("type") == kind) and not self.blockers(c)]
        if not backlog:
            raise MakoError("no unclaimed, unblocked issues in the backlog"
                            + (f" of type {kind}" if kind else ""))
        backlog.sort(key=lambda c: (pos.get(c.id, len(pos)), c.id))
        return backlog[0]

    # public API

    def new(self, kind, title, description="", depends=None, human=False, publish_to=None,
            criteria=None, merge_to=None, agent=None):
        return self.transact(lambda: self._new(kind, title, description, depends, human, publish_to,
                                               criteria, merge_to, agent))

    def claim(self, cid, agent):
        return self.transact(lambda: self._claim(cid, agent))

    def claim_next(self, agent, kind=None):
        return self.transact(lambda: self._claim(self._next_backlog(kind).id, agent))

    def close(self, cid, agent=None, note="", force=False, gate=False):
        # The pre_close_hook runs between two transactions, not under the board lock: a slow build
        # would hold up every other command and outlast the lock's staleness timeout. The close
        # then refuses a branch that moved meanwhile. Only a human's --force skips it.
        unchecked = force and not gate and agent not in self.agents
        checked, notes = "", []
        if self.hooks["pre_close"] and not unchecked:
            target = self.transact(lambda: self._pre_close_target(cid, agent, force, gate))
            if target:
                self.run_pre_close(*target)
                checked, notes = target[2], self.notes
        card = self.transact(lambda: self._close(cid, agent, note, force, gate, checked, unchecked))
        self.notes[:0] = notes
        return card

    def review(self, cid, agent=None):
        return self.transact(lambda: self._review(cid, agent))

    def post_review(self, cid, reviewer, sha, message, score=0, comments=None, files=None):
        # Under the board lock, like request_review, so an upload can't land between the checks and the post.
        return self.transact(lambda: self._post_review(cid, reviewer, sha, message, score, comments, files))

    def release(self, cid, agent=None, note="", force=False):
        return self.transact(lambda: self._release(cid, agent, note, force))

    def discard(self, cid, agent=None, note="", force=False):
        return self.transact(lambda: self._discard(cid, agent, note, force))

    def reopen(self, cid):
        return self.transact(lambda: self._reopen(cid))

    def prune(self, ids=None, dry_run=False, discarded=False):
        return self.transact(lambda: self._prune(ids, dry_run, discarded))

    def edit(self, cid, title=None, kind=None, description=None, append=None,
             depends=None, add_deps=None, remove_deps=None, human=None, publish_to=None,
             criteria=None, merge_to=None):
        return self.transact(lambda: self._edit(cid, title, kind, description, append,
                                                depends, add_deps, remove_deps, human, publish_to,
                                                criteria, merge_to))

    def sync(self):
        return self.transact(lambda: None)


# --- formatting ----------------------------------------------------------------

def fmt_issue_list(m: Mako, status: str | None = None, include_done: bool = False) -> str:
    """The board's columns, then (with include_done) Done and the discarded issues."""
    cards = m.cards()
    lines = []
    for st, name in STATES.items():
        if status and st != status:
            continue
        if st in ("done", "discarded") and not (include_done or status == st):
            continue
        lane = [c for c in cards if c.status == st]
        if st == "discarded" and not lane and status != st:
            continue
        lines.append(f"{name} ({len(lane)})")
        for c in lane:
            extra = "  [human]" if c.human else ""
            if not c.approved:
                extra += "  [unapproved]"
            if st == "in-progress" and not c.human:
                extra += f"  [{c.meta.get('claimed_by')}] {c.meta.get('branch')}"
                if m.waiting_for_review(c):
                    extra += "  [in review]"
            elif st == "done" and c.meta.get("merged"):
                extra += "  [merged]"
            elif st == "done" and c.meta.get("type") == "spike" and not c.human:
                extra += "  [not merged]"  # spikes never merge
            if dep := fmt_deps(m, c):
                extra += f"  [{dep}]"
            lines.append(f"  {c.id}  {c.meta.get('type', ''):<7}  {c.title}{extra}")
    return "\n".join(lines)


def fmt_deps(m: Mako, card: Card) -> str:
    deps = m.deps(card)
    if not deps:
        return ""
    blocked = m.blockers(card)
    state = f"blocked by {', '.join(blocked)}" if blocked else "unblocked"
    return f"depends on {', '.join(deps)}: {state}"


def cli_criteria(a) -> list[str] | None:
    """The --criteria values plus the lines of --criteria-file, or None when neither was given."""
    if a.criteria is None and not a.criteria_file:
        return None
    found = list(a.criteria or [])
    if a.criteria_file:
        try:
            found += Path(a.criteria_file).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise MakoError(f"can't read {a.criteria_file}: {e.strerror}") from None
    return found


def cli_comments(a) -> list[dict]:
    """The --inline comments (PATH:LINE=TEXT, or PATH=TEXT for the file) plus --comments-file's."""
    found = []
    for raw in a.inline or []:
        where, sep, text = raw.partition("=")
        path, colon, line = where.rpartition(":")
        if not sep or not where.strip():
            raise MakoError(f"bad --inline '{raw}': use PATH:LINE=TEXT, or PATH=TEXT for the whole file")
        if colon and line.strip().isdigit():
            found.append({"path": path, "line": int(line), "message": text})
        else:
            found.append({"path": where, "line": None, "message": text})
    if a.comments_file:
        try:
            loaded = json.loads(Path(a.comments_file).read_text(encoding="utf-8"))
        except OSError as e:
            raise MakoError(f"can't read {a.comments_file}: {e.strerror}") from None
        except ValueError as e:
            raise MakoError(f"{a.comments_file} is not JSON: {e}") from None
        if not isinstance(loaded, list):
            raise MakoError(f'{a.comments_file} must hold a list: [{{"path": .., "line": .., "message": ..}}]')
        found += loaded
    return found


def review_count(card: Card) -> int:
    """Reviews held on the issue; a missing or blank field (older cards) is 0."""
    try:
        return max(0, int(str(card.meta.get("reviews", "")).strip() or 0))
    except ValueError:
        return 0


def fmt_show(m: Mako, card: Card) -> str:
    text = card.path.read_text(encoding="utf-8")
    if not str(card.meta.get("reviews", "")).strip():  # older card: show the implied 0
        text = text.replace("\n---\n", "\nreviews: 0\n---\n", 1)
    if dep := fmt_deps(m, card):
        text = text.rstrip("\n") + f"\n\n({card.id} {dep})\n"
    if card.meta.get("type") == "spike":
        target = m.publish_target(card)
        if not target:
            where = "not published (publish_to is empty)"
        elif card.status == "done":
            where = f"published to {target}" if target.is_file() else f"not published to {target}"
        else:
            where = f"copied to {target} on close"
        text = text.rstrip("\n") + f"\n\n(spike summary: {m.summary_path(card)} on its branch; {where})\n"
    return text


def fmt_claim(info: dict) -> str:
    summary = f"\n  summary:  {info['summary']}" if info.get("summary") else ""
    return (f"Claimed {info['id']}: {info['title']}\n"
            f"  branch:   {info['branch']}\n"
            f"  worktree: {info['worktree']}\n"
            f"  card:     {info['card']}{summary}")


def fmt_close(m: Mako, card: Card) -> str:
    if card.meta.get("type") == "spike":
        branch = card.meta.get("branch")
        kept = f"; branch kept: {branch}" if branch and branch_exists(m.project, branch) else ""
        return f"Closed spike {card.id} without merging{kept}."
    if card.meta.get("merged"):
        branch = card.meta.get("branch")
        kept = "; branch kept" if branch and branch_exists(m.project, branch) else ""
        into = card.meta.get("merge_to") or "the base branch"
        return f"Merged {branch} into {into} and closed {card.id}{kept}."
    return f"Closed {card.id} (nothing to merge)."


def fmt_slots(m: Mako) -> str:
    lines = []
    for agent in m.agents:
        wt = m.worktree(agent)
        if not wt.is_dir():
            lines.append(f"{agent:<10} MISSING worktree {wt}")
            continue
        branch = current_branch(wt) or f"(detached at {git(wt, 'rev-parse', '--short', 'HEAD')})"
        dirty = "" if is_clean(wt) else "  (uncommitted changes)"
        lock = m.lock_of(agent)
        lines.append(f"{agent:<10} {lock['id'] if lock else 'idle':<10} {branch}{dirty}")
    return "\n".join(lines)


# --- MCP server (stdio, newline-delimited JSON-RPC) ------------------------------

# Everything agents are told lives in .mako/prompts/*.txt (shipped as templates/prompts/)
# so users can edit it. The guide is common + worker or orchestrator (+ review) + cli.

def review_cli(m: Mako) -> str:
    """The guide's CLI line for the review step: only what this workspace has."""
    if not m.reviewing:
        return ""
    if m.manual_review:
        return "`mako review <ID>` (show the change and record it as reviewed),\n"
    if not m.review_backend:
        return "`mako review <ID>` (reopen the review tool),\n"
    found = ["`mako review <ID>` (send it for review again)"]
    if m.review_has_status:
        found.append("`mako review <ID> --status`")
    if m.review_has_comment:
        found.append('`mako review <ID> --comment ".." [--score N] [--inline path:line=text].. '
                     "[--comments-file f.json] [--files a,b] [--sha S]` (post a review of another slot's "
                     "issue, as you, or as `human` outside an agent pane)")
    if m.backend_hook("setup") is not None:
        found.append("`mako review setup` (set the review backend up)")
    return ", ".join(found) + ",\n"


def guide(m: Mako, agent: str | None) -> str:
    values = {"root": m.root, "project": m.config["project"], "base": m.base, "prefix": m.prefix,
              "agent": agent, "agents": ", ".join(m.agents),
              "worktree": m.worktree(agent) if agent else "",
              "review_cli": review_cli(m),
              "pre_close_commands": "".join(f"    {cmd}\n" for cmd in m.hooks["pre_close"])}
    if agent not in m.agents:
        names = ["common", "orchestrator", "cli"]
    else:
        # A backend can bring its own section: .mako/prompts/review-<backend>.txt.
        backend = f"review-{m.review_backend}"
        names = ["common", "worker", *(["pre-close"] if m.hooks["pre_close"] else []),
                 *(["review"] if m.reviewing else []),
                 *([backend] if m.review_backend and (m.dir / "prompts" / f"{backend}.txt").is_file() else []),
                 *(["review-agents"] if m.review_has_comment else []),
                 "cli"]
    return "\n\n".join(prompt(m, n, values) for n in names) + "\n"


def prompt(m: Mako, name: str, values: dict) -> str:
    """The user-editable text in .mako/prompts/<name>.txt, read fresh each time.
    Only the guide's own placeholders are substituted (plain replace, so other
    braces pass through). A missing file says so instead of hiding it."""
    path = m.dir / "prompts" / f"{name}.txt"
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
    except FileNotFoundError:
        return (f"**`.mako/prompts/{name}.txt` is missing, so your instructions can't be shown. "
                "Tell the user to run `mako init` to restore it.**")
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value or ""))
    return text


# create_issue's description, per role: who needs the user's approval is the part agents misread.
CREATE_ISSUE_HELP = {
    "orchestrator": "Create a new issue in the Backlog. You are the orchestrator, so issues you create are "
                    "already approved and can be claimed straight away. Only issues created by a worker "
                    "slot start unapproved, until the user sets `approved: true` on the card.",
    "worker": "Create a new issue in the Backlog. You are a worker slot, so issues you create start "
              "unapproved: nobody can claim them until the user sets `approved: true` on the card.",
}


def approval_note(card: Card) -> str:
    """The create result's suffix, so the creator never has to infer whether the card is approved."""
    if card.approved:
        return " (approved: it can be claimed straight away)"
    return " (not approved yet: the user must set `approved: true` on the card before anyone can claim it)"


def mcp_tools(review: bool, worker: bool, status: bool, comment: bool) -> list[dict]:
    tools = [t for t in MCP_TOOLS if (review or t["name"] != "request_review")
             and (status or t["name"] != "review_status")
             and ((comment and worker) or t["name"] != "post_review")]
    role = "worker" if worker else "orchestrator"
    return [{**t, "description": CREATE_ISSUE_HELP[role]} if t["name"] == "create_issue" else t
            for t in tools]


MCP_TOOLS = [
    {"name": "guide", "description": "How Mako works and the rules for your role. Read this first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "whoami", "description": "Your agent slot, worktree and current claim.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_issues", "description": "List issues on the kanban board.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": list(STATES), "description": "Only this column."}}}},
    {"name": "get_issue", "description": "Read an issue card (frontmatter and body).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "create_issue", "description": CREATE_ISSUE_HELP["orchestrator"],
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string", "enum": list(TYPES)},
         "title": {"type": "string"},
         "description": {"type": "string",
                         "description": "What and why. A '## Acceptance criteria' heading in it is "
                                        "lifted into the card's criteria section."},
         "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                 "description": "One string per criterion; becomes the card's "
                                                "Acceptance criteria checklist."},
         "depends_on": {"type": "array", "items": {"type": "string"},
                        "description": "Issues that must be Done before this one can be claimed."},
         "human": {"type": "boolean",
                   "description": "A task for the user, not for agents: agents can't claim or close it; "
                                  "the user moves it on the board. Agent issues can depend on it."},
         "publish_to": {"type": "string",
                        "description": "Spikes only: folder that closing copies the spike's summary into "
                                       "(relative to the workspace root). Omit for the config default."},
         "merge_to": {"type": "string",
                      "description": "Not for spikes: closing merges the issue branch into this branch "
                                     "(created from the base branch if missing) instead of the base "
                                     "branch. Omit for the base branch."}},
         "required": ["type", "title"]}},
    {"name": "edit_issue",
     "description": "Change a Backlog issue's title, type or description, or append a note. "
                    "Refused once the issue is claimed (In Progress) or closed (Done), except a spike's "
                    "publish_to, which can also change while it is In Progress.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "title": {"type": "string"},
         "type": {"type": "string", "enum": list(TYPES)},
         "description": {"type": "string",
                         "description": "Replaces the card's Description section (a '## Acceptance "
                                        "criteria' heading in it replaces the criteria too)."},
         "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                 "description": "Replaces the card's Acceptance criteria checklist, "
                                                "one string per criterion ([] resets it)."},
         "append": {"type": "string", "description": "Text added to the end of the card."},
         "depends_on": {"type": "array", "items": {"type": "string"},
                        "description": "Replaces the dependency list ([] clears it)."},
         "add_depends": {"type": "array", "items": {"type": "string"}},
         "remove_depends": {"type": "array", "items": {"type": "string"}},
         "human": {"type": "boolean", "description": "Mark (true) or unmark (false) it as the user's own task."},
         "publish_to": {"type": "string",
                        "description": "Spikes only: folder the summary is copied into on close ('' clears)."},
         "merge_to": {"type": "string",
                      "description": "Branch that closing merges into instead of the base branch "
                                     "('' clears). Not for spikes."}},
         "required": ["id"]}},
    {"name": "claim_issue",
     "description": "Claim an issue for your slot: locks it, checks out its branch in your worktree, "
                    "moves it to In Progress.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "claim_next", "description": "Claim the top unclaimed Backlog issue.",
     "inputSchema": {"type": "object", "properties": {"type": {"type": "string", "enum": list(TYPES)}}}},
    {"name": "close_issue",
     "description": "Finish your issue: merges your branch into the base branch, moves the card to Done "
                    "and frees your slot (worktree goes back to detached base). First commit everything "
                    "and run `git merge <base>` in your worktree, resolving any conflicts; close refuses "
                    "a branch that is behind the base branch. A spike never merges: commit its summary "
                    "file first; closing copies it to the card's publish_to, if set.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "note": {"type": "string", "description": "What changed and how it was tested."}},
         "required": ["id", "note"]}},
    {"name": "release_issue", "description": "Give an issue back to the Backlog without finishing it.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "note": {"type": "string", "description": "Why, and how far you got."}},
         "required": ["id"]}},
    {"name": "request_review",
     "description": "Open the configured review tool on your claimed issue's branch for the user "
                    "(or send it to the review backend; with neither, it tells you what to show the "
                    "user), and record the commit it covers. Commit first. Then end your turn and wait "
                    "for the user to approve or send comments.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "review_status",
     "description": "The issue's review from the review backend, as JSON (e.g. state, votes, messages and "
                    "inline comments). Call it after the user says they have reviewed. Mako adds `sha` "
                    "(the uploaded commit), `parent` (its merge base with the base branch) and `author`.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "post_review",
     "description": "Review another slot's issue when the user asks you to: post a message, a score and "
                    "inline comments to its review, as your slot. First call review_status and read the "
                    "change with `git diff <parent> <sha>` and `git show <sha>:<path>`; pass that sha. "
                    "Refused for your own claim and for an outdated sha. Your review is advisory: only "
                    "the user approves.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "sha": {"type": "string", "description": "The `sha` from review_status: the upload you reviewed."},
         "message": {"type": "string", "description": "Your overall review."},
         "score": {"type": "integer", "enum": [-1, 0, 1],
                   "description": "-1 = must change, 0 = comments only, +1 = looks good to me."},
         "comments": {"type": "array",
                      "description": "Inline comments on the new version of files the change modifies.",
                      "items": {"type": "object", "properties": {
                          "path": {"type": "string"},
                          "line": {"type": "integer",
                                   "description": "Line in the file at sha; omit for the whole file."},
                          "message": {"type": "string"}},
                          "required": ["path", "message"]}},
         "files": {"type": "array", "items": {"type": "string"},
                   "description": "Only if you reviewed just these files: the message says so, comments must "
                                  "be on them, and the score can't be +1."}},
         "required": ["id", "sha", "message"]}},
    {"name": "discard_issue",
     "description": "Drop an issue that won't be done: moves the card to kanban/discarded and off the board "
                    "without merging. A claim is freed but its branch is kept. Workers may discard only "
                    "their own claim or an unclaimed Backlog issue. Relay any warnings to the user.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "note": {"type": "string", "description": "Why it is being dropped."}},
         "required": ["id"]}},
]


def mcp_call(name: str, args: dict, agent: str | None) -> str:
    m = Mako(find_root())
    if name == "guide":
        return guide(m, agent)
    if name == "whoami":
        if agent in m.agents:
            lock = m.lock_of(agent)
            claim = f"{lock['id']} on {lock['branch']}" if lock else "none"
            return f"agent: {agent}\nworktree: {m.worktree(agent)}\ncurrent claim: {claim}"
        return f"agent: {agent or '(none)'} (not a worker slot)\nworkspace: {m.root}"
    if name == "list_issues":
        m.sync()
        return fmt_issue_list(m, args.get("status"), include_done=True)
    if name == "get_issue":
        card = m.get(args["id"])
        return fmt_show(m, card)
    if name == "create_issue":
        card = m.new(args["type"], args["title"], args.get("description", ""), args.get("depends_on"),
                     bool(args.get("human")), args.get("publish_to"), args.get("acceptance_criteria"),
                     args.get("merge_to"), agent)
        result = f"Created {card.id}: {card.title}{approval_note(card)}"
    elif name == "edit_issue":
        keys = ("title", "type", "description", "append", "depends_on", "add_depends", "remove_depends",
                "human", "publish_to", "acceptance_criteria", "merge_to")
        fields = [args.get(k) for k in keys]
        if all(v is None for v in fields):
            raise MakoError(f"nothing to change: pass {', '.join(keys)}")
        card = m.edit(args["id"], *fields)
        result = f"Edited {card.id}: {card.title}"
    elif name == "claim_issue":
        result = fmt_claim(m.claim(args["id"], agent))
    elif name == "claim_next":
        result = fmt_claim(m.claim_next(agent, args.get("type")))
    elif name in ("close_issue", "release_issue") and (card := m.get(args["id"])).human:
        verb = "close" if name == "close_issue" else "release"
        raise MakoError(f"{card.id} is a human task (human: true); agents can't {verb} it. "
                        "The user moves it on the board.")
    elif name == "close_issue":
        result = fmt_close(m, m.close(args["id"], agent, args.get("note", ""), gate=True))
    elif name == "request_review":
        info = m.review(args["id"], agent)
        if info.get("manual"):
            result = (f"Recorded the review of {info['id']} ({info['sha'][:7]}). No review tool is configured, "
                      "so show the user the change yourself: summarise what changed and why, name the branch "
                      f"`{info['branch']}` and include this `git diff {info['base']}...{info['branch']} --stat`:\n"
                      f"{info['stat'] or '(no changes)'}\n"
                      'End your turn and wait. The user will reply "approve" or send comments; on '
                      "comments, fix them, re-run the tests, commit and call request_review again.")
        elif "text" in info:
            then = ("call review_status and address each comment" if m.review_has_status
                    else "fix what they send")
            result = (f"{info['text']}\nEnd your turn and wait. The user will reply \"approve\" or say "
                      f"they have reviewed; then {then}, re-run the tests, commit and call request_review again.")
        else:
            result = (f"Review opened for {info['id']} ({info['sha'][:7]}). End your turn and wait. "
                      'The user will reply "approve" or send comments; on comments, fix them, re-run the '
                      f"tests, commit and call request_review again. Log: {info['log']}")
    elif name == "review_status":
        result = json.dumps(m.review_status(args["id"]), indent=2)
    elif name == "post_review":
        # The reviewer is this server's slot, never an argument: no posting as another slot or the user.
        info = m.post_review(args["id"], agent if agent in m.agents else None, args["sha"], args.get("message", ""),
                             args.get("score", 0), args.get("comments"), args.get("files"))
        result = (f"{info['text']}\nTell the user your review of {info['id']} is posted; its author "
                  f"({info['author']}) reads it with review_status.")
    elif name == "release_issue":
        card = m.release(args["id"], agent, args.get("note", ""))
        result = f"Released {card.id} back to the Backlog."
    elif name == "discard_issue":
        card = m.discard(args["id"], agent, args.get("note", ""))
        result = f"Discarded {card.id}: moved to kanban/discarded, nothing merged."
    else:
        raise MakoError(f"unknown tool {name}")
    return "\n".join([result, *m.notes])


def serve_mcp() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    root = find_root()
    agent = os.environ.get("MAKO_AGENT") or Mako(root).agent_from_cwd()

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    while line := sys.stdin.readline():
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if rid is None:
            continue  # notification
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "mako", "version": VERSION},
                      "instructions": guide(Mako(root), agent)}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            m = Mako(root)
            # Read per call: config.json may change.
            result = {"tools": mcp_tools(m.reviewing, agent in m.agents, m.review_has_status, m.review_has_comment)}
        elif method == "tools/call":
            try:
                text, is_error = mcp_call(params.get("name"), params.get("arguments") or {}, agent), False
            except (MakoError, KeyError) as e:
                text, is_error = f"Error: {e}", True
            except Exception as e:  # keep the server alive whatever happens
                text, is_error = f"Internal error: {type(e).__name__}: {e}", True
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": result})


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


class Init:
    """`mako init`: turn a folder into a Mako workspace. Safe to re-run: it
    upgrades .mako/mako.py and rewrites the generated files, but never touches
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
        """Copy the running mako.py (and its templates) into .mako/."""
        src = Path(__file__).resolve()
        dst = self.dir / "mako.py"
        if not same_path(src, dst):
            self.write(dst, src.read_bytes())
        tpl, dst_tpl = src.parent / "templates", self.dir / "templates"
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


# --- CLI -----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mako", description="Multi-agent kanban for Claude Code.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="make the current folder a Mako workspace (safe to re-run)")
    s.add_argument("project", nargs="?", help="project folder / git repo name (optional on re-run)")
    s.add_argument("--prefix", help="issue id prefix (default: project name upper-cased)")
    s.add_argument("--agents", help="comma-separated worker slots (default: agent-a,agent-b)")
    s.add_argument("--base", help="base branch (default: main)")

    s = sub.add_parser("new", help="create an issue")
    s.add_argument("type", choices=TYPES)
    s.add_argument("title", nargs="+")
    s.add_argument("--body", default="", help="description text")
    s.add_argument("--depends", help="comma-separated issues that must be Done first")
    s.add_argument("--human", action="store_true",
                   help="your own task: agents can't claim it, and it moves freely on the board")
    s.add_argument("--publish-to", help="spikes: folder to copy the summary into on close "
                                        "(default: spike_publish_to in config.json)")
    s.add_argument("--merge-to", help="merge into this branch on close instead of the base branch "
                                      "(created from the base branch if missing; not for spikes)")
    s.add_argument("--criteria", action="append", metavar="TEXT",
                   help="an acceptance criterion (repeatable)")
    s.add_argument("--criteria-file", metavar="FILE", help="acceptance criteria, one per line")

    s = sub.add_parser("list", help="list issues")
    s.add_argument("--all", action="store_true", help="include Done")

    s = sub.add_parser("show", help="print an issue card")
    s.add_argument("id")

    s = sub.add_parser("edit", help="change a Backlog issue (no options: print it)")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--type", choices=TYPES)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--description", help="replace the Description section")
    g.add_argument("--description-file", help="read the new description from a file")
    s.add_argument("--append", help="add text to the end of the card")
    s.add_argument("--criteria", action="append", metavar="TEXT",
                   help="replace the Acceptance criteria with these (repeatable)")
    s.add_argument("--criteria-file", metavar="FILE",
                   help="replace the Acceptance criteria with the lines of a file")
    s.add_argument("--depends", help="replace the dependencies (comma-separated, '' clears)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--human", dest="human", action="store_const", const=True, default=None,
                   help="mark it as your own task (agents can't claim it)")
    g.add_argument("--no-human", dest="human", action="store_const", const=False,
                   help="hand it back to the agents")
    s.add_argument("--publish-to", help="spikes: folder to copy the summary into on close ('' clears); "
                                        "also while In Progress")
    s.add_argument("--merge-to", help="merge into this branch on close instead of the base branch "
                                      "('' clears)")

    for name, text in (("depend", "make an issue wait for others to be Done"),
                       ("undepend", "remove dependencies from an issue")):
        s = sub.add_parser(name, help=text)
        s.add_argument("id")
        s.add_argument("deps", nargs="+")

    s = sub.add_parser("guide", help="print the Mako rules for an agent slot or the orchestrator")
    s.add_argument("--agent", default=os.environ.get("MAKO_AGENT"))

    sub.add_parser("status", help="show agent slots")
    sub.add_parser("sync", help="pick up board changes made in Obsidian")

    s = sub.add_parser("claim", help="claim an issue (id or 'next') for an agent slot")
    s.add_argument("id")
    s.add_argument("--agent", default=os.environ.get("MAKO_AGENT"))
    s.add_argument("--type", choices=TYPES, help="with 'next': only this type")

    for name, text in (("close", "finish an issue, merging its branch into the base branch"),
                       ("release", "return a claimed issue to the backlog")):
        s = sub.add_parser(name, help=text)
        s.add_argument("id")
        s.add_argument("--note", default="")
        s.add_argument("--force", action="store_true", help="act on another slot's claim (close, "
                       "run by a human: also skip the pre_close_hook)")
        s.add_argument("--agent", default=os.environ.get("MAKO_AGENT"))

    s = sub.add_parser("review", help="run the review step on a claimed issue (review_hook, or the review "
                       "backend); `mako review setup` sets the backend up")
    s.add_argument("id", help="the issue, or `setup` to set the review backend up")
    s.add_argument("--status", action="store_true", help="print the backend's report on the review as JSON")
    s.add_argument("--comment", metavar="TEXT", help="post a review of another slot's issue with this message, "
                   "as MAKO_AGENT, or as `human` when it isn't set")
    s.add_argument("--score", type=int, default=0, help="with --comment: -1..1 (as `human`: -2..2)")
    s.add_argument("--inline", action="append", metavar="PATH:LINE=TEXT",
                   help="with --comment: an inline comment (repeatable; PATH=TEXT for the whole file)")
    s.add_argument("--comments-file", metavar="FILE",
                   help='with --comment: inline comments as JSON, [{"path": .., "line": .., "message": ..}]')
    s.add_argument("--files", help="with --comment: you reviewed only these files (comma-separated)")
    s.add_argument("--sha", help="with --comment: the upload you reviewed (default: the latest)")
    s.add_argument("--agent", default=os.environ.get("MAKO_AGENT"))

    s = sub.add_parser("discard", help="drop an issue without merging (kanban/discarded, branch kept)")
    s.add_argument("id")
    s.add_argument("--note", default="")
    s.add_argument("--force", action="store_true",
                   help="act on another slot's claim; commit uncommitted work onto the kept branch")
    s.add_argument("--agent", default=os.environ.get("MAKO_AGENT"))

    s = sub.add_parser("reopen", help="move a closed or discarded issue back to the backlog")
    s.add_argument("id")
    s = sub.add_parser("prune", help="delete the local branches of Done issues (merged ones only)")
    s.add_argument("ids", nargs="*", help="only these issues")
    s.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    s.add_argument("--discarded", action="store_true",
                   help="also delete discarded issues' branches (unmerged work is lost)")
    s = sub.add_parser("launch", help="open the agents in a terminal (Windows Terminal, or Konsole on Linux)")
    s.add_argument("who", nargs="?", help="a single slot, or 'orchestrator'")
    s = sub.add_parser("resume", help="like launch, but each agent continues its last conversation")
    s.add_argument("who", nargs="?", help="a single slot, or 'orchestrator'")
    sub.add_parser("session", help="exit 0 if Claude has a conversation to continue here (agent.cmd)")
    s = sub.add_parser("session-name", help="print the Claude session name for a slot (agent.cmd)")
    s.add_argument("slot", help="an agent slot or 'orchestrator'")
    sub.add_parser("repair", help="re-link the slot worktrees after the workspace moved or changed OS")
    sub.add_parser("mcp", help="run the MCP server on stdio")

    a = p.parse_args(argv)
    if a.cmd == "mcp":
        serve_mcp()
        return 0
    if a.cmd == "session":
        return 0 if has_session(Path.cwd().resolve()) else 1

    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if a.cmd == "init":
            # The current folder, never a workspace found further up.
            print(Init(Path.cwd().resolve()).run(a.project, a.prefix, a.agents, a.base))
            return 0
        m = Mako(find_root())
        if a.cmd == "new":
            card = m.new(a.type, " ".join(a.title), a.body, a.depends, a.human, a.publish_to,
                         cli_criteria(a), a.merge_to, os.environ.get("MAKO_AGENT") or m.agent_from_cwd())
            out = f"Created {card.id}: {card.title}{approval_note(card)}"
        elif a.cmd == "session-name":
            if a.slot != "orchestrator" and a.slot not in m.agents:
                raise MakoError(f"unknown slot '{a.slot}' (slots: orchestrator, {', '.join(m.agents)})")
            out = render_session_name(m.config, a.slot)
        elif a.cmd == "list":
            m.sync()
            out = fmt_issue_list(m, include_done=a.all)
        elif a.cmd == "show":
            m.sync()
            out = fmt_show(m, m.get(a.id))
        elif a.cmd in ("depend", "undepend"):
            add, remove = (a.deps, None) if a.cmd == "depend" else (None, a.deps)
            card = m.edit(a.id, add_deps=add, remove_deps=remove)
            dep = fmt_deps(m, card)
            out = f"{card.id} {dep}" if dep else f"{card.id} has no dependencies"
        elif a.cmd == "edit":
            desc = a.description
            if a.description_file:
                try:
                    desc = Path(a.description_file).read_text(encoding="utf-8")
                except OSError as e:
                    raise MakoError(f"can't read {a.description_file}: {e.strerror}") from None
            criteria = cli_criteria(a)
            if all(v is None for v in (a.title, a.type, desc, a.append, a.depends, a.human, a.publish_to,
                                       criteria, a.merge_to)):
                out = fmt_show(m, m.get(a.id))
            else:
                card = m.edit(a.id, a.title, a.type, desc, a.append, a.depends, human=a.human,
                              publish_to=a.publish_to, criteria=criteria, merge_to=a.merge_to)
                out = f"Edited {card.id}: {card.title}"
        elif a.cmd == "guide":
            out = guide(m, a.agent)
        elif a.cmd == "status":
            m.sync()
            out = fmt_slots(m)
        elif a.cmd == "sync":
            m.sync()
            out = "Board synced"
        elif a.cmd == "claim":
            info = m.claim_next(a.agent, a.type) if a.id == "next" else m.claim(a.id, a.agent)
            out = fmt_claim(info)
        elif a.cmd == "close":
            out = fmt_close(m, m.close(a.id, a.agent, a.note, a.force))
        elif a.cmd == "release":
            card = m.release(a.id, a.agent, a.note, a.force)
            out = f"Released {card.id}"
        elif a.cmd == "review" and a.id == "setup":
            out = m.review_setup()
        elif a.cmd == "review" and a.status:
            out = json.dumps(m.review_status(a.id), indent=2)
        elif a.cmd == "review" and a.comment is not None:
            files = [f.strip() for f in (a.files or "").split(",") if f.strip()]
            info = m.post_review(a.id, a.agent or "human", a.sha, a.comment, a.score, cli_comments(a), files)
            out = info["text"]
        elif a.cmd == "review":
            info = m.review(a.id, a.agent)
            out = (f"Recorded the review of {info['id']} ({info['sha'][:7]}) on {info['branch']}:\n{info['stat']}"
                   if info.get("manual") else info["text"] if "text" in info else
                   f"Opened the review for {info['id']} ({info['sha'][:7]}): {info['command']}\n  log: {info['log']}")
        elif a.cmd == "discard":
            card = m.discard(a.id, a.agent, a.note, a.force)
            out = f"Discarded {card.id}: moved to kanban/discarded, nothing merged."
        elif a.cmd == "reopen":
            out = f"Reopened {m.reopen(a.id).id}"
        elif a.cmd == "prune":
            out = "\n".join(m.prune(a.ids, a.dry_run, a.discarded)) or "Nothing to prune."
        elif a.cmd == "repair":
            out = "\n".join(m.repair())
        elif a.cmd in ("launch", "resume"):
            out = launch(m, a.who, resume=a.cmd == "resume")
        for n in m.notes:
            print(n)
        print(out)
        return 0
    except MakoError as e:
        print(f"mako: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
