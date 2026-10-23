"""The MCP server (stdio, newline-delimited JSON-RPC)."""
from __future__ import annotations

import json
import os
import sys

from mako_cards import STATES, TYPES, Card
from mako_core import Mako, find_root, fmt_claim, fmt_close, fmt_issue_list, fmt_show
from mako_utils import VERSION, MakoError


# Everything agents are told lives in .mako/prompts/*.txt (shipped as templates/prompts/)
# so users can edit it. The MCP initialize instructions are the core: common + worker or
# orchestrator. The rest is in topics (guide_<topic>.txt), which agents read with the guide
# tool; the guide with no topic is the core plus every topic.

# Claude Code's observed truncation point for an MCP server's instructions: past it the
# text is cut off silently, so agents never learn the rest exists.
MCP_INSTRUCTIONS_LIMIT = 4096


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


# The guide topics, the same for everyone. A review backend can add its own section to
# `review`: .mako/prompts/guide_review_<backend>.txt.
TOPICS = ("review", "hooks", "cli")


def topic_names(m: Mako, topic: str) -> list[str]:
    """The prompt files of one topic."""
    if topic != "review":
        return [f"guide_{topic}"]
    backend = f"guide_review_{m.review_backend}"
    return ["guide_review",
            *([backend] if m.review_backend and (m.dir / "prompts" / f"{backend}.txt").is_file() else []),
            *(["guide_review_agents"] if m.review_has_comment else [])]


def prompt_values(m: Mako, agent: str | None) -> dict:
    """The placeholders every prompt file can use."""
    return {"root": m.root, "project": m.config["project"], "base": m.base, "prefix": m.prefix,
            "agent": agent, "agents": ", ".join(m.agents),
            "worktree": m.worktree(agent) if agent else "",
            "review_cli": review_cli(m),
            "pre_close_commands": "".join(f"    {cmd}\n" for cmd in m.hooks["pre_close"]),
            "skip_merge_note": ("\n- Here `skip_base_merge_on_close` is on: closing skips the merge into "
                                f"`{m.base}` (a `merge_to` still merges) and keeps the issue branch."
                                if m.skip_base_merge else "")}


def core_names(m: Mako, agent: str | None) -> list[str]:
    return ["common", "worker" if agent in m.agents else "orchestrator"]


def guide(m: Mako, agent: str | None, topic: str | None = None) -> str:
    """One topic, or with none, the core and every topic."""
    if topic:
        if topic not in TOPICS:
            raise MakoError(f"no guide topic '{topic}'; topics: {', '.join(TOPICS)}")
        names = topic_names(m, topic)
    else:
        names = core_names(m, agent) + [n for t in TOPICS for n in topic_names(m, t)]
    values = prompt_values(m, agent)
    return "\n\n".join(prompt(m, n, values) for n in names) + "\n"


def instructions(m: Mako, agent: str | None) -> str:
    """The MCP initialize instructions: the core for the role. Over the limit (an edited
    prompt), it warns on stderr and at the top, where truncation can't reach."""
    values = prompt_values(m, agent)
    names = core_names(m, agent)
    text = "\n\n".join(prompt(m, n, values) for n in names) + "\n"
    if len(text) > MCP_INSTRUCTIONS_LIMIT:
        files = " and ".join(f".mako/prompts/{n}.txt" for n in names)
        warning = (f"note: {files} give {len(text)} characters of MCP instructions, over Claude Code's "
                   f"{MCP_INSTRUCTIONS_LIMIT}, so the end is cut off. Shorten them.")
        print(f"mako: {warning}", file=sys.stderr)
        text = f"**{warning} The `guide` tool has the full text.**\n\n{text}"
    return text


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
    {"name": "guide",
     "description": "How Mako works and the rules for your role: one topic (your instructions list them), "
                    "or with none, everything.",
     "inputSchema": {"type": "object", "properties": {
         "topic": {"type": "string", "description": "e.g. review, hooks, cli. Omit for everything."}}}},
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
        return guide(m, agent, args.get("topic") or None)
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
                      "instructions": instructions(Mako(root), agent)}
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
