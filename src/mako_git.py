"""git: the plain commands, and the git-heavy parts of the Mako class (GitMixin)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from mako_cards import COLUMNS, SUMMARY_BODY, SUMMARY_FIELDS, Card, _yaml_value
from mako_utils import MakoError, now


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


class GitMixin:
    """Worktrees, merges and branches (mixed into Mako)."""

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

    def _free_slot(self, cid: str, lock: dict, branch: str) -> None:
        """Put the owning slot back on a detached base branch and drop the lock."""
        wt = self.worktree(lock.get("agent", ""))
        if wt.is_dir() and current_branch(wt) == branch:
            if not is_clean(wt):
                raise MakoError(f"{lock.get('agent')}'s worktree has uncommitted changes on {branch}; "
                                "commit or discard them first")
            git(wt, "switch", "--detach", self.base)
        shutil.rmtree(self.locks / cid, ignore_errors=True)

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

    def _drop_temp_worktree(self, path: Path) -> None:
        if path.exists():
            _git(self.project, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
        _git(self.project, "worktree", "prune")

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
