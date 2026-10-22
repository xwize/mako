"""The review step: review backends and request/status/post (ReviewMixin)."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from mako_cards import Card, review_count
from mako_git import _git, branch_exists, git, git_file, is_clean
from mako_utils import MakoError, now, write_atomic


# Review backends. "review": {"backend": "<name>", ...} in config.json loads the workspace's
# .mako/templates/review/<name>.py (shipped in templates/review/, not part of Mako's code). Its
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


class ReviewMixin:
    """The review step (mixed into Mako)."""

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
