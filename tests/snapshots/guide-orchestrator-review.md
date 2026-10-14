# Mako

This workspace uses Mako: a kanban board plus fixed agent slots, each with its own git worktree.

- Workspace root: <WS>
- Project repo: demo/ (main checkout, branch `main`).
- Issues: cards like `DEMO-0001` in kanban/open and kanban/closed, shown on kanban/board.md (Obsidian).
- Lifecycle: Backlog -> In Progress (claim) -> Done (close). Closing merges the issue branch into
  `main`, so nothing reaches Done unmerged, except spikes, which never merge (their branch is
  kept).
- Discarding (`discard_issue`, `mako discard`) drops an issue that won't be done: it leaves the
  board for kanban/discarded, nothing is merged and any branch is kept. `mako reopen` undoes it.
- Mako owns kanban/. Never edit those files directly; use the mako tools. To change a card's
  title, type or description, use `edit_issue` (`mako edit`); it only works while the card is in
  the Backlog.

## You are the orchestrator

You run at the workspace root. Worker slots: agent-a, agent-b.

- Write issues with `create_issue` and refine Backlog ones with `edit_issue`; check progress
  with `list_issues`, `get_issue` and `mako status`.
- When an issue needs another one finished first, give it `depends_on` (on `create_issue`, or
  `add_depends` / `remove_depends` on `edit_issue`). It can't be claimed until those are Done.
- Do not claim issues and do not merge: workers merge their own work when they close.
- `release_issue` returns a stuck issue to the Backlog; `mako reopen <ID>` reopens a closed one.
- `discard_issue` drops an issue the user doesn't want done (any open issue, with a note). Pass
  its warnings on: a kept branch to delete, and dependents to `mako undepend`.

## CLI

`mako` is on PATH in panes started by mako.launch.bat:
`mako list [--all]`, `mako status`, `mako show <ID>`, `mako new <feature|bug|spike> "title"`,
`mako edit <ID> [--title ..] [--type ..] [--description ..|--description-file ..] [--append ..]`,
`mako new .. --depends <ID,..>`, `mako depend <ID> <DEP..>`, `mako undepend <ID> <DEP..>`,
`mako claim <ID|next>`, `mako close <ID> --note "..."`, `mako release <ID>`,
`mako discard <ID> [--note ".."] [--force]`, `mako reopen <ID>` (closed or discarded),
`mako review <ID>` (reopen the review tool),
`mako launch [slot]`, `mako resume [slot]` (reopen the panes, each continuing its last
conversation), `mako guide`, `mako init` (re-run to upgrade the generated files).

