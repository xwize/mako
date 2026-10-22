"""Locks and the board: syncing board.md and the kanban folders, and rendering it (BoardMixin)."""
from __future__ import annotations

import json
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from mako_cards import COLUMNS, STATES, Card, read_card
from mako_utils import MakoError, now, write_atomic


class BoardMixin:
    """Claim locks, the board lock and board.md (mixed into Mako)."""

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
