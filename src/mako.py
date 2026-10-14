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
made in Obsidian are picked up by `sync`, which every command runs first.

Lifecycle: Backlog -> In Progress (claim) -> Done (close). Closing merges the
issue branch into the base branch, so nothing reaches Done unmerged. The worker
first merges the base branch into its own branch and resolves any conflicts in
its worktree; close refuses a branch that is behind the base branch. Spikes are
the exception: they close without merging and their branch is kept.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"
TYPES = ("feature", "bug", "spike")
COLUMNS = {"backlog": "Backlog", "in-progress": "In Progress", "done": "Done"}
FIELDS = ("id", "type", "title", "depends_on", "status", "created", "branch",
          "claimed_by", "claimed", "merged", "closed", "discarded")
LIST_FIELDS = ("depends_on",)


class MakoError(Exception):
    pass


def now() -> str:
    return os.environ.get("MAKO_NOW") or datetime.now().strftime("%Y-%m-%d %H:%M")  # MAKO_NOW: tests


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


def is_clean(cwd: Path) -> bool:
    return git(cwd, "status", "--porcelain") == ""


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


def spawn_detached(cmd: str, cwd: Path, out) -> None:
    """Start a shell command and don't wait for it. Through the shell so things
    like npx (a .cmd on Windows) resolve; stdin is DEVNULL so it can never read
    the MCP server's stdin, and output goes to `out`. On Windows it gets a
    hidden console of its own (console children stay hidden too) and its own
    process group, and leaves the caller's job if allowed, so it outlives the
    MCP server."""
    kw = dict(cwd=str(cwd), shell=True, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    if os.name != "nt":
        subprocess.Popen(cmd, start_new_session=True, **kw)
        return
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:  # the job doesn't allow breakaway
        subprocess.Popen(cmd, creationflags=flags, **kw)


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

- [ ]

## Notes

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


class Mako:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / ".mako"
        self.config = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.project = root / self.config["project"]
        self.prefix = self.config["prefix"].upper()
        self.base = self.config.get("base_branch", "main")
        self.agents = list(self.config.get("agents", []))
        # Empty means no review step: no request_review, no gate on close_issue.
        self.review_command = str(self.config.get("review_command") or "").strip()
        # On: close keeps the merged branch and names it in the merge commit. Off: it's deleted.
        self.keep_branches = self.config.get("keep_branches") is True
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

    # ids & paths

    def norm_id(self, raw: str) -> str:
        m = re.fullmatch(r"\s*(?:([A-Za-z]+)-)?0*(\d+)\s*", str(raw))
        if not m or (m.group(1) and m.group(1).upper() != self.prefix):
            raise MakoError(f"bad issue id '{raw}' (expected {self.prefix}-0001)")
        return f"{self.prefix}-{int(m.group(2)):04d}"

    def worktree(self, agent: str) -> Path:
        return self.dir / "worktrees" / agent

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

    def save(self, card: Card) -> None:
        """Write the card into the folder for its status: open/, closed/ or discarded/."""
        folder = {"done": self.closed_dir, "discarded": self.discarded_dir}.get(card.status, self.open_dir)
        target = folder / f"{card.id}.md"
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
        """Apply what the user did in Obsidian: drags and newly typed cards."""
        self.order, seen = [], set()
        for column, cid, text in self.parse_board():
            if cid is None:
                card = self._create_from_text(text)
                self.order.append(card.id)
                continue
            if cid in seen:
                continue
            seen.add(cid)
            self.order.append(cid)
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
        if column == "in-progress":
            if blocked := self.blockers(card):
                raise MakoError(f"it is blocked by {', '.join(blocked)}")
            raise MakoError("cards only enter In Progress when an agent claims them")
        if column == "backlog":
            if card.status == "done":
                self._reopen(card.id)
            else:
                self._release(card.id, agent=None, note="", force=False)
        elif card.status == "in-progress":
            self._close(card.id, agent=None, note="", force=False)
        else:
            raise MakoError(f"a {COLUMNS[card.status]} card can't move to {COLUMNS[column]}")

    def _create_from_text(self, text: str) -> Card:
        tag = re.search(r"\s#(feature|bug|spike)\b", text)
        text = re.sub(r"\s+#(feature|bug|spike)\b", "", text).strip()
        prefix = re.match(r"(?i)^(feature|bug|spike)\s*[:\-]\s*(.+)$", text)
        if prefix:
            kind, title = prefix.group(1).lower(), prefix.group(2)
        else:
            kind, title = (tag.group(1) if tag else "feature"), text
        card = self._new(kind, title.strip(), "")
        self.note(f"board: created {card.id} from new card '{card.title}'")
        return card

    def card_line(self, card: Card) -> str:
        box = "x" if card.status == "done" else " "
        who = f" ({card.meta['claimed_by']})" if card.status == "in-progress" and card.meta.get("claimed_by") else ""
        blocked = ""
        if card.status == "backlog" and (b := self.blockers(card)):
            blocked = f" #blocked (waiting on {', '.join(x.split(' ')[0] for x in b)})"
        unmerged = " #not-merged" if card.status == "done" and card.meta.get("type") == "spike" else ""
        return f"- [{box}] [[{card.id}]] {card.title} #{card.meta.get('type', 'feature')}{unmerged}{blocked}{who}"

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

    def _new(self, kind: str, title: str, description: str, depends=None) -> Card:
        if kind not in TYPES:
            raise MakoError(f"type must be one of {', '.join(TYPES)}")
        if not title.strip():
            raise MakoError("title is required")
        cid = self.next_id()
        deps = self.parse_ids(depends)
        self.check_deps(cid, deps)
        meta = {"id": cid, "type": kind, "title": title.strip(), "depends_on": deps,
                "status": "backlog", "created": now()}
        card = Card(self.open_dir / f"{cid}.md", meta,
                    CARD_BODY.format(title=title.strip(), description=description.strip() or "_TBD_"))
        self.save(card)
        return card

    def _claim(self, cid: str, agent: str | None) -> dict:
        if not agent:
            raise MakoError("no agent identity: set MAKO_AGENT or pass --agent <slot>")
        if agent not in self.agents:
            raise MakoError(f"'{agent}' is not an agent slot (slots: {', '.join(self.agents)}); "
                            "only worker slots claim issues")
        card = self.get(cid)
        cid = card.id
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
        wt = self.worktree(agent)
        if not wt.is_dir():
            raise MakoError(f"worktree for {agent} is missing: {wt}")
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
        self.save(card)
        return self._claim_info(card, agent)

    def _claim_info(self, card: Card, agent: str) -> dict:
        return {"id": card.id, "title": card.title, "type": card.meta.get("type"),
                "branch": card.meta.get("branch"), "worktree": str(self.worktree(agent)),
                "card": str(card.path)}

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

    def _close(self, cid: str, agent: str | None, note: str, force: bool, gate: bool = False) -> Card:
        """Finish an issue. If its branch has commits they are merged into the
        base branch first; the branch must already contain the base branch, so
        conflicts are resolved by the worker in its own worktree, never here.
        With gate (the agents' close_issue) and a review_command configured, the
        branch's HEAD must be the commit the last request_review covered.
        Spikes never merge: their result is the closing note, and a branch with
        commits is kept as it is (no review gate, no behind-base check)."""
        card = self.get(cid)
        branch = card.meta.get("branch", "")
        spike = card.meta.get("type") == "spike"
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
        pending = unmerged_commits(self.project, branch, self.base)
        if gate and self.review_command and lock and pending and not force and not spike:
            head = git(self.project, "rev-parse", branch)
            if lock.get("reviewed") != head:
                raise MakoError(f"call `request_review` first (no review for {head[:7]})")
        if pending and not spike:
            self._merge_into_base(card.id, branch)
        if lock:
            self._free_slot(card.id, lock, branch)
        # A branch that never had commits is always deleted: there is nothing to keep.
        # With keep_branches, one that was merged (now or before a reopen) is kept for `mako prune`.
        keep = pending if spike else self.keep_branches and (pending or card.meta.get("merged"))
        if branch and branch_exists(self.project, branch) and not keep:
            git(self.project, "branch", "-d", branch)  # fully merged (or never had commits)
        if pending and not spike:
            card.meta["merged"] = now()
        if spike:
            kept = f"; branch kept: {branch}" if pending else ""
            note = f"Spike closed without merging{kept}.\n\n{note.strip()}"
        card.meta.update(status="done", closed=now())
        self._append(card, "Closing note", agent, note)
        self.save(card)
        return card

    def _merge_into_base(self, cid: str, branch: str) -> None:
        if not git_ok(self.project, "merge-base", "--is-ancestor", self.base, branch):
            raise MakoError(f"{branch} is behind {self.base}. In your worktree run `git merge {self.base}`, "
                            f"resolve any conflicts, run the tests and commit, then close {cid} again")
        if current_branch(self.project) != self.base:
            raise MakoError(f"the main checkout {self.project} must be on {self.base} to merge")
        if not is_clean(self.project):
            raise MakoError(f"the main checkout {self.project} has uncommitted changes; "
                            "it must be clean to merge")
        try:
            title = self.get(cid).title
            message = f"Merge {branch} ({cid}): {title}" if self.keep_branches else f"Merge {cid}: {title}"
            git(self.project, "merge", "--no-ff", "-m", message, branch)
        except MakoError as e:
            _git(self.project, "merge", "--abort")
            raise MakoError(f"merging {branch} into {self.base} failed, nothing was changed: {e}") from None

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
        return card

    def _review(self, cid: str, agent: str | None) -> dict:
        """Launch review_command for a claimed issue, detached, and record the
        commit it covers on the claim (cleared with the claim on close/release)."""
        if not self.review_command:
            raise MakoError('no review step: set "review_command" in .mako/config.json')
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
        cmd = self.review_command
        for key, value in {"base": self.base, "branch": branch, "id": cid, "worktree": str(wt)}.items():
            cmd = cmd.replace("{" + key + "}", value)
        log = self.dir / "logs" / f"{cid}.review.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as out:
            out.write(f"# {now()}  {cid} at {head}\n# cwd: {wt}\n$ {cmd}\n")
            out.flush()
            spawn_detached(cmd, wt, out)
        lock.update(reviewed=head, review_requested=now())
        (self.locks / cid / "owner.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
        return {"id": cid, "sha": head, "log": str(log), "command": cmd}

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

    def _reopen(self, cid: str) -> Card:
        card = self.get(cid)
        if card.status not in ("done", "discarded"):
            raise MakoError(f"{card.id} is not closed or discarded")
        was = card.status
        shutil.rmtree(self.locks / card.id, ignore_errors=True)
        card.meta.update(status="backlog", claimed_by="", claimed="", closed="", discarded="")
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
              depends=None, add_deps=None, remove_deps=None) -> Card:
        """Change a Backlog card's title, type, body or dependencies. Holds the issue's claim
        lock while checking and writing, so a claim and an edit can't overlap."""
        card = self.get(cid)
        cid = card.id
        if card.status in ("done", "discarded"):
            state = "closed" if card.status == "done" else "discarded"
            raise MakoError(f"{cid} is {state}; `mako reopen {cid}` it first")
        if kind is not None and kind not in TYPES:
            raise MakoError(f"type must be one of {', '.join(TYPES)}")
        if title is not None and not title.strip():
            raise MakoError("title can't be empty")
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
                card.body = re.sub(rf"(?m)^# {re.escape(old)}[ \t]*$",
                                   lambda _: f"# {title.strip()}", card.body, count=1)
            if kind is not None:
                card.meta["type"] = kind
            if description is not None:
                text = description.strip() or "_TBD_"
                section = re.compile(r"(?ms)^(## Description[ \t]*\n).*?(?=^## |\Z)")
                if section.search(card.body):
                    card.body = section.sub(lambda mm: f"{mm.group(1)}\n{text}\n\n", card.body, count=1)
                else:
                    card.body = f"\n# {card.title}\n\n## Description\n\n{text}\n"
            if append is not None and append.strip():
                card.body = card.body.rstrip("\n") + f"\n\n{append.strip()}\n"
            if depends is not None or add_deps or remove_deps:
                deps = self.parse_ids(depends) if depends is not None else self.deps(card)
                deps += [d for d in self.parse_ids(add_deps) if d not in deps]
                gone = self.parse_ids(remove_deps)
                deps = [d for d in deps if d not in gone]
                self.check_deps(cid, deps)
                card.meta["depends_on"] = deps
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
            unmerged = unmerged_commits(self.project, branch, self.base)
            lost = f", losing {unmerged} unmerged commit{'s' * (unmerged != 1)}" if unmerged else ""
            if card.status == "done" and unmerged:
                spike = " (a spike's branch)" if card.meta.get("type") == "spike" else ""
                lines.append(f"skipped {label}: not fully merged, {unmerged} commit"
                             f"{'s' * (unmerged != 1)} not in {self.base}{spike}")
                continue
            if dry_run:
                lines.append(f"would delete {label}{lost}")
                continue
            try:
                git(self.project, "branch", "-D" if card.status == "discarded" else "-d", branch)
            except MakoError as e:
                lines.append(f"skipped {label}: {e}")
                continue
            lines.append(f"deleted {label}{lost}")
        return lines

    def _next_backlog(self, kind: str | None) -> Card:
        pos = {cid: i for i, cid in enumerate(self.order)}
        backlog = [c for c in self.cards()
                   if c.status == "backlog" and not self.read_lock(c.id)
                   and (kind is None or c.meta.get("type") == kind) and not self.blockers(c)]
        if not backlog:
            raise MakoError("no unclaimed, unblocked issues in the backlog"
                            + (f" of type {kind}" if kind else ""))
        backlog.sort(key=lambda c: (pos.get(c.id, len(pos)), c.id))
        return backlog[0]

    # public API

    def new(self, kind, title, description="", depends=None):
        return self.transact(lambda: self._new(kind, title, description, depends))

    def claim(self, cid, agent):
        return self.transact(lambda: self._claim(cid, agent))

    def claim_next(self, agent, kind=None):
        return self.transact(lambda: self._claim(self._next_backlog(kind).id, agent))

    def close(self, cid, agent=None, note="", force=False, gate=False):
        return self.transact(lambda: self._close(cid, agent, note, force, gate))

    def review(self, cid, agent=None):
        return self.transact(lambda: self._review(cid, agent))

    def release(self, cid, agent=None, note="", force=False):
        return self.transact(lambda: self._release(cid, agent, note, force))

    def discard(self, cid, agent=None, note="", force=False):
        return self.transact(lambda: self._discard(cid, agent, note, force))

    def reopen(self, cid):
        return self.transact(lambda: self._reopen(cid))

    def prune(self, ids=None, dry_run=False, discarded=False):
        return self.transact(lambda: self._prune(ids, dry_run, discarded))

    def edit(self, cid, title=None, kind=None, description=None, append=None,
             depends=None, add_deps=None, remove_deps=None):
        return self.transact(lambda: self._edit(cid, title, kind, description, append,
                                                depends, add_deps, remove_deps))

    def sync(self):
        return self.transact(lambda: None)

    def slots(self) -> list[dict]:
        out = []
        for agent in self.agents:
            wt = self.worktree(agent)
            info = {"agent": agent, "worktree": str(wt), "exists": wt.is_dir()}
            if wt.is_dir():
                info["branch"] = current_branch(wt) or f"(detached at {git(wt, 'rev-parse', '--short', 'HEAD')})"
                info["clean"] = is_clean(wt)
            lock = self.lock_of(agent)
            info["issue"] = lock["id"] if lock else None
            out.append(info)
        return out


# --- formatting ----------------------------------------------------------------

def fmt_issue_list(m: Mako, status: str | None = None, include_done: bool = False) -> str:
    """The board's columns, then (with include_done) Done and the discarded issues."""
    cards = m.cards()
    lines = []
    for st, name in [*COLUMNS.items(), ("discarded", "Discarded")]:
        if status and st != status:
            continue
        if st in ("done", "discarded") and not (include_done or status == st):
            continue
        lane = [c for c in cards if c.status == st]
        if st == "discarded" and not lane and status != st:
            continue
        lines.append(f"{name} ({len(lane)})")
        for c in lane:
            extra = ""
            if st == "in-progress":
                extra = f"  [{c.meta.get('claimed_by')}] {c.meta.get('branch')}"
            elif st == "done" and c.meta.get("merged"):
                extra = "  [merged]"
            elif st == "done" and c.meta.get("type") == "spike":
                extra = "  [not merged]"  # spikes never merge
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


def fmt_show(m: Mako, card: Card) -> str:
    text = card.path.read_text(encoding="utf-8")
    if dep := fmt_deps(m, card):
        text = text.rstrip("\n") + f"\n\n({card.id} {dep})\n"
    return text


def fmt_claim(info: dict) -> str:
    return (f"Claimed {info['id']}: {info['title']}\n"
            f"  branch:   {info['branch']}\n"
            f"  worktree: {info['worktree']}\n"
            f"  card:     {info['card']}")


def fmt_close(m: Mako, card: Card) -> str:
    if card.meta.get("type") == "spike":
        branch = card.meta.get("branch")
        kept = f"; branch kept: {branch}" if branch and branch_exists(m.project, branch) else ""
        return f"Closed spike {card.id} without merging{kept}."
    if card.meta.get("merged"):
        branch = card.meta.get("branch")
        kept = "; branch kept" if branch and branch_exists(m.project, branch) else ""
        return f"Merged {branch} into the base branch and closed {card.id}{kept}."
    return f"Closed {card.id} (nothing to merge)."


def fmt_slots(m: Mako) -> str:
    lines = []
    for s in m.slots():
        if not s["exists"]:
            lines.append(f"{s['agent']:<10} MISSING worktree {s['worktree']}")
            continue
        dirty = "" if s["clean"] else "  (uncommitted changes)"
        lines.append(f"{s['agent']:<10} {s['issue'] or 'idle':<10} {s['branch']}{dirty}")
    return "\n".join(lines)


# --- MCP server (stdio, newline-delimited JSON-RPC) ------------------------------

# Everything agents are told lives in .mako/prompts/*.txt (shipped as templates/prompts/)
# so users can edit it. The guide is common + worker or orchestrator (+ review) + cli.

def guide(m: Mako, agent: str | None) -> str:
    values = {"root": m.root, "project": m.config["project"], "base": m.base, "prefix": m.prefix,
              "agent": agent, "agents": ", ".join(m.agents),
              "worktree": m.worktree(agent) if agent else "",
              # Review text appears only when review_command is set; otherwise the guide is as before.
              "review_cli": "`mako review <ID>` (reopen the review tool),\n" if m.review_command else ""}
    if agent not in m.agents:
        names = ["common", "orchestrator", "cli"]
    else:
        names = ["common", "worker", *(["review"] if m.review_command else []), "cli"]
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


MCP_TOOLS = [
    {"name": "guide", "description": "How Mako works and the rules for your role. Read this first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "whoami", "description": "Your agent slot, worktree and current claim.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_issues", "description": "List issues on the kanban board.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": [*COLUMNS, "discarded"], "description": "Only this column."}}}},
    {"name": "get_issue", "description": "Read an issue card (frontmatter and body).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "create_issue", "description": "Create a new issue in the Backlog.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string", "enum": list(TYPES)},
         "title": {"type": "string"},
         "description": {"type": "string"},
         "depends_on": {"type": "array", "items": {"type": "string"},
                        "description": "Issues that must be Done before this one can be claimed."}},
         "required": ["type", "title"]}},
    {"name": "edit_issue",
     "description": "Change a Backlog issue's title, type or description, or append a note. "
                    "Refused once the issue is claimed (In Progress) or closed (Done).",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "title": {"type": "string"},
         "type": {"type": "string", "enum": list(TYPES)},
         "description": {"type": "string", "description": "Replaces the card's Description section."},
         "append": {"type": "string", "description": "Text added to the end of the card."},
         "depends_on": {"type": "array", "items": {"type": "string"},
                        "description": "Replaces the dependency list ([] clears it)."},
         "add_depends": {"type": "array", "items": {"type": "string"}},
         "remove_depends": {"type": "array", "items": {"type": "string"}}},
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
                    "a branch that is behind the base branch.",
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
     "description": "Open the configured review tool on your claimed issue's branch for the user, "
                    "and record the commit it covers. Commit first. Then end your turn and wait for "
                    "the user to approve or send comments.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
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
        card = m.new(args["type"], args["title"], args.get("description", ""), args.get("depends_on"))
        result = f"Created {card.id}: {card.title}"
    elif name == "edit_issue":
        keys = ("title", "type", "description", "append", "depends_on", "add_depends", "remove_depends")
        fields = [args.get(k) for k in keys]
        if all(v is None for v in fields):
            raise MakoError(f"nothing to change: pass {', '.join(keys)}")
        card = m.edit(args["id"], *fields)
        result = f"Edited {card.id}: {card.title}"
    elif name == "claim_issue":
        result = fmt_claim(m.claim(args["id"], agent))
    elif name == "claim_next":
        result = fmt_claim(m.claim_next(agent, args.get("type")))
    elif name == "close_issue":
        result = fmt_close(m, m.close(args["id"], agent, args.get("note", ""), gate=True))
    elif name == "request_review":
        info = m.review(args["id"], agent)
        result = (f"Review opened for {info['id']} ({info['sha'][:7]}). End your turn and wait. "
                  'The user will reply "approve" or send comments; on comments, fix them, re-run the '
                  f"tests, commit and call request_review again. Log: {info['log']}")
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
            review = bool(Mako(root).review_command)  # read per call: config.json may change
            result = {"tools": [t for t in MCP_TOOLS if review or t["name"] != "request_review"]}
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

def launch(m: Mako, who: str | None, resume: bool = False) -> str:
    """Open the agents in Windows Terminal. With resume, each one continues its
    latest conversation (agent.cmd ... resume -> claude -c)."""
    if os.name != "nt":
        raise MakoError("launch currently supports Windows Terminal only")
    extra = ["resume"] if resume else []
    verb = "Resumed" if resume else "Opened"
    if who is None:
        subprocess.Popen(["cmd", "/c", str(m.root / "mako.launch.bat"), *extra], cwd=m.root,
                         creationflags=subprocess.DETACHED_PROCESS)
        return f"{verb} Windows Terminal with all agents"
    if who != "orchestrator" and who not in m.agents:
        raise MakoError(f"unknown slot '{who}'")
    cwd = m.root if who == "orchestrator" else m.worktree(who)
    subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", who, "--suppressApplicationTitle",
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

AGENT_CMD = """\
@echo off
rem Starts Claude for one mako slot:  agent.cmd <slot> [resume]
rem "orchestrator" runs at the workspace root; worker slots run in their worktree.
rem With "resume", Claude continues the slot's latest conversation (claude -c);
rem Claude keys conversations by directory, so each slot gets its own back.
setlocal
set "SLOT=%~1"
if "%SLOT%"=="" (
  echo usage: agent.cmd ^<slot^|orchestrator^> [resume]
  exit /b 1
)
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "MAKO_AGENT=%SLOT%"
set "PATH=%ROOT%\\.mako\\bin;%PATH%"
title %SLOT%

if /i "%SLOT%"=="orchestrator" (
  set "DIR=%ROOT%"
  set "ADD=%ROOT%\\{project}"
) else (
  set "DIR=%ROOT%\\.mako\\worktrees\\%SLOT%"
  set "ADD=%ROOT%"
)
cd /d "%DIR%"

rem Only continue when there is a conversation to continue; otherwise start fresh.
set "CONTINUE="
if /i "%~2"=="resume" (
  python "%~dp0mako.py" session >nul 2>&1 && set "CONTINUE=-c"
)
if /i "%~2"=="resume" if not defined CONTINUE echo No earlier conversation for %SLOT%; starting a new one.

claude %CONTINUE% --add-dir "%ADD%" --mcp-config "%ROOT%\\.mako\\agents\\%SLOT%.mcp.json" --allowedTools mcp__mako
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


def launch_bat(agents: list[str]) -> str:
    # Pane ids follow creation order: orchestrator 0, then slot k is pane k.
    # Each slot splits the pane to its left vertically; wt's --size is the share
    # the new pane takes, so pane k-1 hands over (n-k+1)/(n-k+2) of its width,
    # leaving every column 1/(n+1) of the window.
    pane = '--title {0} --suppressApplicationTitle -d "{1}" cmd /k "%AGENT%" {0} %*'
    cmds = ["new-tab " + pane.format("orchestrator", "%ROOT%")]
    n = len(agents)
    for k, slot in enumerate(agents, 1):
        cmds.append(f"focus-pane -t {k - 1}")
        size = (n - k + 1) / (n - k + 2)
        cmds.append(f"split-pane -V --size {size:.3g} " + pane.format(slot, f"%WT%\\{slot}"))
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


def mcp_config(script: Path, agent: str) -> str:
    server = {"command": "python", "args": [script.as_posix(), "mcp"], "env": {"MAKO_AGENT": agent}}
    return json.dumps({"mcpServers": {"mako": server}}, indent=2) + "\n"


# .mako/prompts/worker.finish.txt as the first version of MAKO-0012 shipped it
# (CRLF -> LF, outer newlines stripped). worker.txt replaced it; init removes it if unedited.
LEGACY_WORKER_FINISH_SHA256 = "d0aef799393329d25053f2fd53d7f9f29aefa5ab0541acce7e6960d5104efcbe"


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
                        f"  Run mako.launch.bat to start the orchestrator and {', '.join(agents) or 'no slots'}.\n"
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
            config.setdefault("review_command", "")  # listed so they can be found; never changed
            config.setdefault("keep_branches", False)
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
                "review_command": "", "keep_branches": False}

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
            ident = [] if _git(repo, "config", "user.email").stdout.strip() else \
                ["-c", "user.name=Mako", "-c", "user.email=mako@localhost"]
            git(repo, *ident, "commit", "-q", "--allow-empty", "-m", "Initial commit")
            self.log.append(f"made an initial commit on {base} in {repo.name}/")
        if not branch_exists(repo, base):
            raise MakoError(f"{repo.name}/ has no branch '{base}'; create it or pass --base <branch>")
        if current_branch(repo) != base:
            self.log.append(f"warning: {repo.name}/ is not on {base}; closing issues needs it there")

    def worktrees(self, repo: Path, base: str, agents: list[str]) -> None:
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
        script = self.dir / "mako.py"
        self.write(self.root / "mako.cmd", ROOT_CMD, crlf=True)
        self.write(self.root / "mako.launch.bat", launch_bat(agents), crlf=True)
        self.write(self.dir / "agent.cmd", AGENT_CMD.format(project=project), crlf=True)
        self.write(self.dir / "bin" / "mako.cmd", BIN_CMD, crlf=True)
        self.write(self.dir / "bin" / "mako", BIN_SH)
        os.chmod(self.dir / "bin" / "mako", 0o755)
        for agent in ["orchestrator", *agents]:
            self.write(self.dir / "agents" / f"{agent}.mcp.json", mcp_config(script, agent))

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
        self.retire_prompt("worker.finish.txt", LEGACY_WORKER_FINISH_SHA256, "worker.txt")

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

    def retire_prompt(self, name: str, default_sha256: str, successor: str) -> None:
        """A prompt file an older Mako installed: deleted if it is still the shipped
        default, otherwise kept (it's the user's work) with a note to move it."""
        path = self.dir / "prompts" / name
        if not path.is_file():
            return
        if text_sha256(path.read_bytes()) == default_sha256:
            path.unlink()
            self.log.append(f"removed .mako/prompts/{name} (unchanged default, replaced by {successor})")
        else:
            self.log.append(f"note: .mako/prompts/{name} is no longer used and was left as it is; "
                            f"move your changes into .mako/prompts/{successor}, then delete it")

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
    s.add_argument("--depends", help="replace the dependencies (comma-separated, '' clears)")

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
        s.add_argument("--force", action="store_true", help="act on another slot's claim")
        s.add_argument("--agent", default=os.environ.get("MAKO_AGENT"))

    s = sub.add_parser("review", help="open the review_command from config.json on a claimed issue")
    s.add_argument("id")
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
    s = sub.add_parser("launch", help="open Windows Terminal with the agents")
    s.add_argument("who", nargs="?", help="a single slot, or 'orchestrator'")
    s = sub.add_parser("resume", help="like launch, but each agent continues its last conversation")
    s.add_argument("who", nargs="?", help="a single slot, or 'orchestrator'")
    sub.add_parser("session", help="exit 0 if Claude has a conversation to continue here (agent.cmd)")
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
            card = m.new(a.type, " ".join(a.title), a.body, a.depends)
            out = f"Created {card.id}: {card.title}"
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
            if all(v is None for v in (a.title, a.type, desc, a.append, a.depends)):
                out = fmt_show(m, m.get(a.id))
            else:
                card = m.edit(a.id, a.title, a.type, desc, a.append, a.depends)
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
        elif a.cmd == "review":
            info = m.review(a.id, a.agent)
            out = f"Opened the review for {info['id']} ({info['sha'][:7]}): {info['command']}\n  log: {info['log']}"
        elif a.cmd == "discard":
            card = m.discard(a.id, a.agent, a.note, a.force)
            out = f"Discarded {card.id}: moved to kanban/discarded, nothing merged."
        elif a.cmd == "reopen":
            out = f"Reopened {m.reopen(a.id).id}"
        elif a.cmd == "prune":
            out = "\n".join(m.prune(a.ids, a.dry_run, a.discarded)) or "Nothing to prune."
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
