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

## You are worker slot `agent-a`

Your worktree is <WS>\.mako\worktrees\agent-a. Change code only there, never in demo/ (the main checkout) or
another slot's worktree, and never run git commands in demo/.

1. Claim before working: `claim_issue` (or `claim_next`). This checks out the issue branch
   `<type>/<ID>-<slug>` in your worktree. You hold one issue at a time. Issues whose
   `depends_on` aren't all Done are blocked: claiming them fails and `claim_next` skips them.
2. Read the card with `get_issue`.
3. Commit on the issue branch, starting messages with the id: `DEMO-0002: short summary`.
4. To finish:
   1. Commit everything.
   2. Run `git merge main` in your worktree. Resolve any conflicts there, keeping both your
      change and what landed on `main`, then commit.
   3. Run the tests.
   4. Call `close_issue` with a note on what changed and how you tested it.
   It merges your branch into `main`, moves the card to Done and frees your slot. If it says your
   branch is behind `main`, another worker merged first: repeat steps 2-4.
   For a spike, finishing is: commit everything, then call `close_issue` with your findings as the
   note. A spike never merges, so there is no `git merge main`; its branch is kept.
5. If you cannot finish, commit what you have and call `release_issue` with a note. The branch is
   kept and the next claim resumes it.
6. If the user decides an issue won't be done, call `discard_issue` with a note. You can discard
   your own claim or an unclaimed Backlog issue, never another slot's. Pass any warnings it
   returns (kept branch, blocked dependents) on to the user.

## CLI

`mako` is on PATH in panes started by mako.launch.bat:
`mako list [--all]`, `mako status`, `mako show <ID>`, `mako new <feature|bug|spike> "title"`,
`mako edit <ID> [--title ..] [--type ..] [--description ..|--description-file ..] [--append ..]`,
`mako new .. --depends <ID,..>`, `mako depend <ID> <DEP..>`, `mako undepend <ID> <DEP..>`,
`mako claim <ID|next>`, `mako close <ID> --note "..."`, `mako release <ID>`,
`mako discard <ID> [--note ".."] [--force]`, `mako reopen <ID>` (closed or discarded),
`mako launch [slot]`, `mako resume [slot]` (reopen the panes, each continuing its last
conversation), `mako guide`, `mako init` (re-run to upgrade the generated files).

