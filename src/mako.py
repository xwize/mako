#!/usr/bin/env python3
"""mako - Multi-Agent Kanban for Obsidian.

Lives in <workspace>/.mako/mako.py, the entry point (CLI and `mako mcp`), with the
modules it imports next to it. Standard library only.
    mako_utils   errors, time, text and process helpers
    mako_cards   card files: frontmatter, sections, acceptance criteria
    mako_git     git commands, and the git-heavy issue steps (GitMixin)
    mako_board   locks, and syncing/rendering board.md (BoardMixin)
    mako_hooks   the user's hooks and config defaults (HooksMixin)
    mako_review  the review step and review backends (ReviewMixin)
    mako_core    the Mako class (issue operations) and text formatting
    mako_mcp     the MCP server
    mako_init    launching the agents, and `mako init`

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
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing the modules next to this file must not leave __pycache__ in .mako/

from mako_cards import TYPES  # noqa: E402
from mako_core import (Mako, find_root, fmt_claim, fmt_close, fmt_deps, fmt_issue_list, fmt_show,  # noqa: E402
                       fmt_slots, render_session_name)
from mako_init import Init, has_session, launch  # noqa: E402
from mako_mcp import approval_note, guide, serve_mcp  # noqa: E402
from mako_utils import MakoError  # noqa: E402


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
