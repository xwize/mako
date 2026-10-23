"""Hooks: the user's commands at points in an issue's life (HooksMixin), and config defaults."""
from __future__ import annotations

import os
import re
from pathlib import Path

from mako_cards import Card
from mako_git import branch_exists, git, is_clean
from mako_utils import MakoError, now, run_and_wait, spawn_detached


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
                   "pre_close_timeout": 0, "keep_branches": False,
                   "skip_base_merge_on_close": False, "spike_publish_to": "",
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


class HooksMixin:
    """Running hooks (mixed into Mako)."""

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
