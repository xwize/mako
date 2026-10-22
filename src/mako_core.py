"""The workspace: the Mako class (issue operations) and its text formatting."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path

from mako_board import BoardMixin
from mako_cards import (CARD_BODY, COLUMNS, EMPTY_CRITERIA, SPIKE_DIR, STATES, TYPES, Card, criteria_lines,
                        cycle_from, read_card, review_count, set_section, split_criteria)
from mako_git import (GitMixin, _git, branch_exists, check_merge_to, current_branch, git, is_broken_worktree,
                      is_clean, unmerged_commits)
from mako_hooks import CONFIG_DEFAULTS, HOOK_EVENTS, HooksMixin, hook_commands
from mako_review import ReviewMixin
from mako_utils import MakoError, minutes_between, now, slugify, write_atomic


def _config_int(config: dict, key: str) -> int:
    """A non-negative integer setting; missing, malformed or negative values fall back to the default."""
    default = CONFIG_DEFAULTS[key]
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


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


class Mako(BoardMixin, ReviewMixin, HooksMixin, GitMixin):

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

    # the /clear nudge

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
