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
its worktree; close refuses a branch that is behind the base branch.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"
TYPES = ("feature", "bug", "spike")
COLUMNS = {"backlog": "Backlog", "in-progress": "In Progress", "done": "Done"}
FIELDS = ("id", "type", "title", "status", "created", "branch",
          "claimed_by", "claimed", "merged", "closed")


class MakoError(Exception):
    pass


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "issue"


def write_atomic(path: Path, text: str) -> None:
    # Dot-prefixed temp file so Obsidian never indexes it. Retry the replace
    # because Obsidian can briefly hold the target open on Windows.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
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
                          text=True, stdin=subprocess.DEVNULL)


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
    v = "" if value is None else str(value)
    if v and (re.search(r"[:#\[\]{}\"'\\,&*!|>%@`]", v) or v != v.strip()):
        return json.dumps(v, ensure_ascii=False)
    return v


def read_card(path: Path) -> Card:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return Card(path, {"id": path.stem}, text)
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        meta[key.strip()] = value
    return Card(path, meta, m.group(2))


CARD_BODY = """
# {title}

## Description

{description}

## Acceptance criteria

- [ ]

## Notes

"""


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
        self.kanban = root / "kanban"
        self.open_dir = self.kanban / "open"
        self.closed_dir = self.kanban / "closed"
        self.board = self.kanban / "board.md"
        self.locks = self.dir / "locks"
        self.notes: list[str] = []
        self.order: list[str] = []
        self.settings: str | None = None
        for d in (self.open_dir, self.closed_dir, self.locks):
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
        for d in (self.open_dir, self.closed_dir):
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
        paths = sorted(self.open_dir.glob(f"{self.prefix}-*.md")) + \
            sorted(self.closed_dir.glob(f"{self.prefix}-*.md"))
        return [read_card(p) for p in paths]

    def next_id(self) -> str:
        nums = [int(m.group(1)) for d in (self.open_dir, self.closed_dir)
                for p in d.glob(f"{self.prefix}-*.md")
                if (m := re.fullmatch(rf"{self.prefix}-(\d+)", p.stem))]
        return f"{self.prefix}-{max(nums, default=0) + 1:04d}"

    def save(self, card: Card, closed: bool) -> None:
        target = (self.closed_dir if closed else self.open_dir) / f"{card.id}.md"
        write_atomic(target, card.render())
        if card.path != target and card.path.exists():
            card.path.unlink()
        card.path = target

    def note(self, msg: str) -> None:
        self.notes.append(msg)

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
        return [self.read_lock(d.name) for d in sorted(self.locks.iterdir())
                if d.is_dir() and not d.name.startswith("_")]

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
                                    "(delete it if no mako command is running)")
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
            if card.status == column:
                continue
            try:
                self._apply_drag(card, column)
                self.note(f"board: {cid} moved to {COLUMNS[column]}")
            except MakoError as e:
                self.note(f"board: can't move {cid} to {COLUMNS[column]}: {e} (moved it back)")

    def _apply_drag(self, card: Card, column: str) -> None:
        if column == "in-progress":
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
        text = re.sub(r"\s+#(feature|bug|spike)\b", "", text).strip()
        m = re.match(r"(?i)^(feature|bug|spike)\s*[:\-]\s*(.+)$", text)
        kind, title = (m.group(1).lower(), m.group(2)) if m else ("feature", text)
        card = self._new(kind, title.strip(), "")
        self.note(f"board: created {card.id} from new card '{card.title}'")
        return card

    def card_line(self, card: Card) -> str:
        box = "x" if card.status == "done" else " "
        who = f" ({card.meta['claimed_by']})" if card.status == "in-progress" and card.meta.get("claimed_by") else ""
        return f"- [{box}] [[{card.id}]] {card.title} #{card.meta.get('type', 'feature')}{who}"

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

    def _new(self, kind: str, title: str, description: str) -> Card:
        if kind not in TYPES:
            raise MakoError(f"type must be one of {', '.join(TYPES)}")
        if not title.strip():
            raise MakoError("title is required")
        cid = self.next_id()
        meta = {"id": cid, "type": kind, "title": title.strip(), "status": "backlog", "created": now()}
        card = Card(self.open_dir / f"{cid}.md", meta,
                    CARD_BODY.format(title=title.strip(), description=description.strip() or "_TBD_"))
        self.save(card, closed=False)
        return card

    def _claim(self, cid: str, agent: str | None) -> dict:
        if not agent:
            raise MakoError("no agent identity: set MAKO_AGENT or pass --agent <slot>")
        if agent not in self.agents:
            raise MakoError(f"'{agent}' is not an agent slot (slots: {', '.join(self.agents)}); "
                            "only worker slots claim issues")
        card = self.get(cid)
        cid = card.id
        if card.status == "done":
            raise MakoError(f"{cid} is closed; reopen it first")
        lock = self.read_lock(cid)
        if lock and lock.get("agent") == agent:
            return self._claim_info(card, agent)
        if lock:
            raise MakoError(f"{cid} is already claimed by {lock.get('agent')}")
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
        self.save(card, closed=False)
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

    def _close(self, cid: str, agent: str | None, note: str, force: bool) -> Card:
        """Finish an issue. If its branch has commits they are merged into the
        base branch first; the branch must already contain the base branch, so
        conflicts are resolved by the worker in its own worktree, never here."""
        card = self.get(cid)
        branch = card.meta.get("branch", "")
        if card.status == "done":
            raise MakoError(f"{card.id} is already closed")
        lock = self.read_lock(card.id)
        wt = self.worktree(lock.get("agent", "")) if lock else None
        if lock:
            self._check_owner(card.id, lock, agent, force)
            if wt.is_dir() and current_branch(wt) == branch and not is_clean(wt):
                raise MakoError(f"{lock.get('agent')}'s worktree has uncommitted changes on {branch}; "
                                "commit them first")
        pending = unmerged_commits(self.project, branch, self.base)
        if pending:
            self._merge_into_base(card.id, branch)
        if lock:
            self._free_slot(card.id, lock, branch)
        if branch and branch_exists(self.project, branch):
            git(self.project, "branch", "-d", branch)  # fully merged (or never had commits)
        if pending:
            card.meta["merged"] = now()
        card.meta.update(status="done", closed=now())
        self._append(card, "Closing note", agent, note)
        self.save(card, closed=True)
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
            git(self.project, "merge", "--no-ff", "-m", f"Merge {cid}: {self.get(cid).title}", branch)
        except MakoError as e:
            _git(self.project, "merge", "--abort")
            raise MakoError(f"merging {branch} into {self.base} failed, nothing was changed: {e}") from None

    def _release(self, cid: str, agent: str | None, note: str, force: bool) -> Card:
        """Back to the Backlog without finishing. The branch is kept, so the
        next claim resumes it."""
        card = self.get(cid)
        if card.status == "done":
            raise MakoError(f"{card.id} is closed")
        lock = self.read_lock(card.id)
        if lock:
            self._check_owner(card.id, lock, agent, force)
            self._free_slot(card.id, lock, card.meta.get("branch", ""))
        card.meta.update(status="backlog", claimed_by="", claimed="")
        self._append(card, "Released", agent, note)
        self.save(card, closed=False)
        return card

    def _reopen(self, cid: str) -> Card:
        card = self.get(cid)
        if card.status != "done":
            raise MakoError(f"{card.id} is not closed")
        shutil.rmtree(self.locks / card.id, ignore_errors=True)
        card.meta.update(status="backlog", claimed_by="", claimed="", closed="")
        self.save(card, closed=False)
        return card

    def _next_backlog(self, kind: str | None) -> Card:
        pos = {cid: i for i, cid in enumerate(self.order)}
        backlog = [c for c in self.cards()
                   if c.status == "backlog" and not self.read_lock(c.id)
                   and (kind is None or c.meta.get("type") == kind)]
        if not backlog:
            raise MakoError("no unclaimed issues in the backlog" + (f" of type {kind}" if kind else ""))
        backlog.sort(key=lambda c: (pos.get(c.id, len(pos)), c.id))
        return backlog[0]

    # public API

    def new(self, kind, title, description=""):
        return self.transact(lambda: self._new(kind, title, description))

    def claim(self, cid, agent):
        return self.transact(lambda: self._claim(cid, agent))

    def claim_next(self, agent, kind=None):
        return self.transact(lambda: self._claim(self._next_backlog(kind).id, agent))

    def close(self, cid, agent=None, note="", force=False):
        return self.transact(lambda: self._close(cid, agent, note, force))

    def release(self, cid, agent=None, note="", force=False):
        return self.transact(lambda: self._release(cid, agent, note, force))

    def reopen(self, cid):
        return self.transact(lambda: self._reopen(cid))

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
    cards = m.cards()
    lines = []
    for st, name in COLUMNS.items():
        if status and st != status:
            continue
        if st == "done" and not (include_done or status == "done"):
            continue
        lane = [c for c in cards if c.status == st]
        lines.append(f"{name} ({len(lane)})")
        for c in lane:
            extra = ""
            if st == "in-progress":
                extra = f"  [{c.meta.get('claimed_by')}] {c.meta.get('branch')}"
            elif st == "done" and c.meta.get("merged"):
                extra = "  [merged]"
            lines.append(f"  {c.id}  {c.meta.get('type', ''):<7}  {c.title}{extra}")
    return "\n".join(lines)


def fmt_claim(info: dict) -> str:
    return (f"Claimed {info['id']}: {info['title']}\n"
            f"  branch:   {info['branch']}\n"
            f"  worktree: {info['worktree']}\n"
            f"  card:     {info['card']}")


def fmt_close(card: Card) -> str:
    if card.meta.get("merged"):
        return f"Merged {card.meta.get('branch')} into the base branch and closed {card.id}."
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

GUIDE_COMMON = """\
# Mako

This workspace uses Mako: a kanban board plus fixed agent slots, each with its own git worktree.

- Workspace root: {root}
- Project repo: {project}/ (main checkout, branch `{base}`).
- Issues: cards like `{prefix}-0001` in kanban/open and kanban/closed, shown on kanban/board.md (Obsidian).
- Lifecycle: Backlog -> In Progress (claim) -> Done (close). Closing merges the issue branch into
  `{base}`, so nothing reaches Done unmerged.
- Mako owns kanban/. Never edit those files directly; use the mako tools.
"""

GUIDE_WORKER = """
## You are worker slot `{agent}`

Your worktree is {worktree}. Change code only there, never in {project}/ (the main checkout) or
another slot's worktree, and never run git commands in {project}/.

1. Claim before working: `claim_issue` (or `claim_next`). This checks out the issue branch
   `<type>/<ID>-<slug>` in your worktree. You hold one issue at a time.
2. Read the card with `get_issue`.
3. Commit on the issue branch, starting messages with the id: `{prefix}-0002: short summary`.
4. To finish:
   1. Commit everything.
   2. Run `git merge {base}` in your worktree. Resolve any conflicts there, keeping both your
      change and what landed on `{base}`, then commit.
   3. Run the tests.
   4. Call `close_issue` with a note on what changed and how you tested it.
   It merges your branch into `{base}`, moves the card to Done and frees your slot. If it says your
   branch is behind `{base}`, another worker merged first: repeat steps 2-4.
5. If you cannot finish, commit what you have and call `release_issue` with a note. The branch is
   kept and the next claim resumes it.
"""

GUIDE_ORCHESTRATOR = """
## You are the orchestrator

You run at the workspace root. Worker slots: {agents}.

- Write and refine issues with `create_issue`; check progress with `list_issues`, `get_issue`
  and `mako status`.
- Do not claim issues and do not merge: workers merge their own work when they close.
- `release_issue` returns a stuck issue to the Backlog; `mako reopen <ID>` reopens a closed one.
"""

GUIDE_CLI = """
## CLI

`mako` is on PATH in panes started by mako.launch.bat:
`mako list [--all]`, `mako status`, `mako show <ID>`, `mako new <feature|bug|spike> "title"`,
`mako claim <ID|next>`, `mako close <ID> --note "..."`, `mako release <ID>`, `mako reopen <ID>`,
`mako guide`, `mako init` (re-run to upgrade the generated files).
"""


def guide(m: "Mako", agent: str | None) -> str:
    values = {"root": m.root, "project": m.config["project"], "base": m.base, "prefix": m.prefix,
              "agent": agent, "agents": ", ".join(m.agents),
              "worktree": m.worktree(agent) if agent else ""}
    role = GUIDE_WORKER if agent in m.agents else GUIDE_ORCHESTRATOR
    return (GUIDE_COMMON + role + GUIDE_CLI).format(**values)


MCP_TOOLS = [
    {"name": "guide", "description": "How Mako works and the rules for your role. Read this first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "whoami", "description": "Your agent slot, worktree and current claim.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_issues", "description": "List issues on the kanban board.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": list(COLUMNS), "description": "Only this column."}}}},
    {"name": "get_issue", "description": "Read an issue card (frontmatter and body).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "create_issue", "description": "Create a new issue in the Backlog.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string", "enum": list(TYPES)},
         "title": {"type": "string"},
         "description": {"type": "string"}}, "required": ["type", "title"]}},
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
        return card.path.read_text(encoding="utf-8")
    if name == "create_issue":
        card = m.new(args["type"], args["title"], args.get("description", ""))
        result = f"Created {card.id}: {card.title}"
    elif name == "claim_issue":
        result = fmt_claim(m.claim(args["id"], agent))
    elif name == "claim_next":
        result = fmt_claim(m.claim_next(agent, args.get("type")))
    elif name == "close_issue":
        result = fmt_close(m.close(args["id"], agent, args.get("note", "")))
    elif name == "release_issue":
        card = m.release(args["id"], agent, args.get("note", ""))
        result = f"Released {card.id} back to the Backlog."
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
            result = {"tools": MCP_TOOLS}
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

def launch(m: Mako, who: str | None) -> str:
    if os.name != "nt":
        raise MakoError("launch currently supports Windows Terminal only")
    if who is None:
        subprocess.Popen(["cmd", "/c", str(m.root / "mako.launch.bat")], cwd=m.root,
                         creationflags=subprocess.DETACHED_PROCESS)
        return "Opened Windows Terminal with all agents"
    if who != "orchestrator" and who not in m.agents:
        raise MakoError(f"unknown slot '{who}'")
    cwd = m.root if who == "orchestrator" else m.worktree(who)
    subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", who, "--suppressApplicationTitle",
                      "-d", str(cwd), "cmd", "/k", str(m.dir / "agent.cmd"), who],
                     creationflags=subprocess.DETACHED_PROCESS)
    return f"Opened a tab for {who}"


# --- init ------------------------------------------------------------------------

CLAUDE_POINTER = ("This workspace uses Mako for issues and multi-agent work; the `mako` MCP server's "
                  "instructions (and its `guide` tool) explain your role and the rules.\n")

ROOT_CMD = '@python "%~dp0.mako\\mako.py" %*\n'
BIN_CMD = '@python "%~dp0..\\mako.py" %*\n'
BIN_SH = """\
#!/bin/sh
# POSIX shim so `mako` works from Git Bash (Claude Code's shell and `!` commands).
exec python "$(dirname "$0")/../mako.py" "$@"
"""

AGENT_CMD = """\
@echo off
rem Starts Claude for one mako slot:  agent.cmd <slot>
rem "orchestrator" runs at the workspace root; worker slots run in their worktree.
setlocal
set "SLOT=%~1"
if "%SLOT%"=="" (
  echo usage: agent.cmd ^<slot^|orchestrator^>
  exit /b 1
)
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "MAKO_AGENT=%SLOT%"
set "PATH=%ROOT%\\.mako\\bin;%PATH%"
title %SLOT%

if /i "%SLOT%"=="orchestrator" (
  cd /d "%ROOT%"
  claude --add-dir "%ROOT%\\{project}" --mcp-config "%ROOT%\\.mako\\agents\\orchestrator.mcp.json" --allowedTools mcp__mako
) else (
  cd /d "%ROOT%\\.mako\\worktrees\\%SLOT%"
  claude --add-dir "%ROOT%" --mcp-config "%ROOT%\\.mako\\agents\\%SLOT%.mcp.json" --allowedTools mcp__mako
)
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
    pane = '--title {0} --suppressApplicationTitle -d "{1}" cmd /k "%AGENT%" {0}'
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
    row = lambda cells: "rem   |" + "|".join(c.center(width) for c in cells) + "|"
    blank = row([""] * len(names))
    diagram = "\n".join([rule, blank, row(names), blank, rule])
    panes = "\n".join(f"rem   pane {k}: {slot}" for k, slot in enumerate(agents, 1)) or "rem   (no slots)"
    return LAUNCH_BAT.format(diagram=diagram, panes=panes, commands=" ; ^\n  ".join(cmds))


def mcp_config(script: Path, agent: str) -> str:
    server = {"command": "python", "args": [script.as_posix(), "mcp"], "env": {"MAKO_AGENT": agent}}
    return json.dumps({"mcpServers": {"mako": server}}, indent=2) + "\n"


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
        for d in ("kanban/open", "kanban/closed", ".mako/bin", ".mako/agents",
                  ".mako/locks", ".mako/worktrees"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.install()
        self.worktrees(repo, base, agents)
        self.generate(project, agents)
        self.obsidian(project)
        if not (self.root / "CLAUDE.md").exists():
            self.write(self.root / "CLAUDE.md", f"{CLAUDE_POINTER}\n# {project}\n")
        Mako(self.root).sync()  # renders kanban/board.md from the cards
        self.log.append(f"\nMako workspace ready: {self.root}\n"
                        f"  Open this folder as an Obsidian vault to see kanban/board.md.\n"
                        f"  Run mako.launch.bat to start the orchestrator and {', '.join(agents) or 'no slots'}.")
        return "\n".join(self.log)

    def config(self, project, prefix, agents, base) -> dict:
        path = self.dir / "config.json"
        if path.is_file():
            config = json.loads(path.read_text(encoding="utf-8"))
            if project and project != config["project"]:
                raise MakoError(f"this workspace's project is '{config['project']}', not '{project}'")
            if any(v is not None for v in (prefix, agents, base)):
                self.log.append("kept existing .mako/config.json (edit it to change prefix, agents or base)")
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
        return {"project": project, "prefix": prefix, "base_branch": base or "main", "agents": agents}

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

    s = sub.add_parser("list", help="list issues")
    s.add_argument("--all", action="store_true", help="include Done")

    s = sub.add_parser("show", help="print an issue card")
    s.add_argument("id")

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

    s = sub.add_parser("reopen", help="move a closed issue back to the backlog")
    s.add_argument("id")
    s = sub.add_parser("launch", help="open Windows Terminal with the agents")
    s.add_argument("who", nargs="?", help="a single slot, or 'orchestrator'")
    sub.add_parser("mcp", help="run the MCP server on stdio")

    a = p.parse_args(argv)
    if a.cmd == "mcp":
        serve_mcp()
        return 0

    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if a.cmd == "init":
            # The current folder, never a workspace found further up.
            print(Init(Path.cwd().resolve()).run(a.project, a.prefix, a.agents, a.base))
            return 0
        m = Mako(find_root())
        if a.cmd == "new":
            card = m.new(a.type, " ".join(a.title), a.body)
            out = f"Created {card.id}: {card.title}"
        elif a.cmd == "list":
            m.sync()
            out = fmt_issue_list(m, include_done=a.all)
        elif a.cmd == "show":
            out = m.get(a.id).path.read_text(encoding="utf-8")
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
            out = fmt_close(m.close(a.id, a.agent, a.note, a.force))
        elif a.cmd == "release":
            card = m.release(a.id, a.agent, a.note, a.force)
            out = f"Released {card.id}"
        elif a.cmd == "reopen":
            out = f"Reopened {m.reopen(a.id).id}"
        elif a.cmd == "launch":
            out = launch(m, a.who)
        for n in m.notes:
            print(n)
        print(out)
        return 0
    except MakoError as e:
        print(f"mako: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
