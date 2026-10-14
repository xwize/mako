# Mako

Multi-Agent Kanban for Obsidian: a quick start for running several Claude Code agents on one project.
You get a kanban board you view and edit in Obsidian, a git repo, one permanent git worktree per agent
slot, issue claiming with locks, and an MCP server so agents can claim, work on and close issues
themselves.

## Install

Open PowerShell in an empty folder that will become the workspace, and run:

```powershell
irm https://raw.githubusercontent.com/xwize/mako/main/install.ps1 | iex
```

It asks for a project name. To pass it (and other options) on the command line instead, use:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/xwize/mako/main/install.ps1))) -Project myproj
```

`irm | iex` can't take arguments, which is why the second form exists. Neither form runs a script
file, so the execution policy doesn't block them and you don't need to change it. Both work in
Windows PowerShell 5.1 and PowerShell 7. On an old Windows PowerShell 5.1 that fails to download
with a TLS or "could not create SSL/TLS secure channel" error, run this first in the same window:

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 'Tls12'
```

### Options

| Option | Meaning |
| --- | --- |
| `-Project <name>` | Project folder and git repo name. Issue ids use it as their prefix: `myproj` gives `MYPROJ-0001`. |
| `-Prefix <LETTERS>` | Issue id prefix, if you want a different one. |
| `-Agents a,b,c` | Worker slot names (default `agent-a,agent-b`). |
| `-Base <branch>` | Base branch (default `main`). |
| `-Ref <branch-or-tag>` | Mako version to install (default `main`). Use a release tag such as `-Ref v0.1.0` to pin. |
| `-Repo <url-or-path>` | Where to get Mako from (default this GitHub repo). A local clone works, for testing. |
| `-Upgrade` | The folder is already a workspace: install the downloaded Mako into it. |
| `-Force` | Don't ask before setting up in a folder that isn't empty. |

The installer installs `main` by default. There are no release tags yet. Once there are, `-Ref`
pins to one.

### Prerequisites

- **Python 3.8 or newer** as `python` on PATH. Mako is a single standard-library Python file,
  and everything it generates runs `python`, so `py -3` alone isn't enough. A `python` that opens
  the Microsoft Store is only a stub. Install from [python.org](https://www.python.org/downloads/)
  and tick *Add python.exe to PATH*.
- **git** ([Git for Windows](https://git-scm.com/download/win)).
- Optional, needed by the launcher: **Windows Terminal** (`wt`) and **Claude Code** (`claude`). The
  installer warns if they're missing but still finishes.

It needs no admin rights and doesn't touch your global PATH. The `mako` shims are put on PATH only
inside the agent panes.

### What it does

1. Checks the prerequisites and stops with a message if Python 3 or git is missing.
2. Shallow-clones Mako (`-Ref`) into a temp folder.
3. Runs `python src/mako.py init <project>` in the current folder.
4. Deletes the temp folder and prints the next steps.

It is safe to run twice. In a folder that already has `.mako/config.json` it stops and changes
nothing, unless you pass `-Upgrade`. An upgrade replaces Mako's code and generated files but never
touches your cards, config or repo history.

### What you get

```
<workspace>/                     open this folder as an Obsidian vault
  CLAUDE.md                      your project's instructions for Claude
  kanban/board.md                the board (Obsidian Kanban plugin)
  kanban/open/, kanban/closed/   one card per issue
  kanban/discarded/              issues dropped with `mako discard` (off the board; `mako reopen` undoes)
  <project>/                     your git repo, on the base branch
  mako.cmd                       the CLI:  .\mako.cmd new feature "title"
  mako.launch.bat                Windows Terminal: orchestrator + one pane per slot
  .obsidian/                     Kanban and Advanced Exclude plugins, pre-enabled
  .mako/                         Mako itself: code, config, locks, slot worktrees
  .mako/prompts/                 everything agents are told; yours to edit (see below)
```

### Editing what agents are told

Each agent's guide (the `mako` MCP server's instructions, also `mako guide`) is built fresh from
the files in `.mako/prompts/`:

| File | Part |
| --- | --- |
| `common.txt` | top of every guide: the workspace and the rules |
| `worker.txt` | a worker slot's role and how to finish an issue |
| `orchestrator.txt` | the orchestrator's role |
| `review.txt` | the code review steps, shown to workers only when `review_command` is set |
| `cli.txt` | the CLI list at the end of every guide |
| `claude-pointer.txt` | the line `mako init` puts at the top of a new workspace's `CLAUDE.md` |

They can use `{root}`, `{project}`, `{base}`, `{prefix}`, `{agent}`, `{agents}` and `{worktree}`;
other braces are left as they are. A deleted file shows a "missing" message in its place, and
`mako init` restores it. On an upgrade, a file you haven't changed follows the new default; one
you have edited is kept, with a note pointing at the new default in `.mako/templates/prompts/`.

### Next steps

1. Open the folder as a vault in Obsidian (*Open folder as vault*) and trust it, so the Kanban plugin
   loads. Open `kanban/board.md`.
2. Make a first issue: `.\mako.cmd new feature "My first feature"`.
3. Run `.\mako.launch.bat`. The orchestrator opens on the left and the worker slots on the right.
   Ask a worker to "pick up MYPROJ-0001".
4. Closed the terminal? `.\mako.cmd resume` reopens the same layout with every pane continuing its
   last Claude conversation (`claude -c`); a pane with no earlier conversation starts fresh.
   `mako resume <slot|orchestrator>` reopens just one tab.

## Moving cards by hand

You can work the board yourself in Obsidian, by dragging cards on `kanban/board.md` or by moving
card files between `kanban/open/`, `kanban/closed/` and `kanban/discarded/`. The next `mako`
command (or agent tool call) picks the change up and prints what it did.

Claiming and closing belong to the agents: dragging a card into In Progress or Done, or moving its
file into `closed/`, is refused and the card snaps back with a message. What you can do by hand:

| Move | Same as |
| --- | --- |
| In Progress back to Backlog (drag) | `mako release` |
| Done back to Backlog (drag), or a file from `closed/` into `open/` | `mako reopen` |
| A file from `discarded/` into `open/` | `mako reopen` |
| A file from `open/` into `discarded/` | `mako discard` (claim freed, branch kept) |
| Up and down within a column | reorders the board |

If a card is moved as a file and dragged on the board before the next sync, the file move wins.
A card found in two folders at once is reported and left alone until you delete the extra copy.

### Your own tasks

Mark a card as yours with `mako new feature "Set up the API key" --human`, `mako edit <ID> --human`,
or by typing it on the board with a `#human` tag. Human cards are a plain kanban: they move to
any column or folder in any direction, with no locks, branches or merges, and agents can't claim
or close them. An agent issue can depend on one; it unblocks when you move it to Done.

## Code review

Mako can stop each issue for your review before it merges. Set `review_command` in
`.mako/config.json` to the tool to open, for example [difit](https://github.com/yoshiko-pg/difit):

```json
"review_command": "npx difit HEAD {base}"
```

When a worker's branch is ready, it opens the review and waits. Reply "approve" in its pane, or
paste your comments and it fixes them and asks again. Nothing merges until the latest commits
have been reviewed. Leave it empty (`""`, the default) to turn review off.

The command can use `{base}`, `{branch}`, `{id}` and `{worktree}`. Its output goes to
`.mako/logs/<ID>.review.log`, and `mako review <ID>` reopens it.

## Development

Mako's code is `src/mako.py` (single file, standard library only) and `scripts/upgrade.py`. It
supports Python 3.8 and newer, so don't use newer syntax or library features. The tools below are
for developing Mako only; it doesn't need them to install or run.

### Running the tests

From the repo root, before committing:

```
python scripts/test.py
```

(`scripts\test.cmd` from cmd or PowerShell.) It runs ruff, then the regression tests in `tests/`
with pytest, and exits non-zero if anything fails. Extra arguments go to pytest, for example
`python scripts/test.py -k claim`. pytest comes from `pip install pytest`, or from `uvx pytest`
if [uv](https://docs.astral.sh/uv/) is installed. ruff is used if installed, or from uv's cache,
and skipped with a note otherwise.

The tests drive the real CLI and MCP server against a fresh workspace per test, in pytest's temp
folder, with the clock (`MAKO_NOW`) and git config fixed, so they never touch a real board and a
run gives the same result every time. `tests/helpers.py` has the helpers for acting as the user
(board drags, typed cards, hand edits) and as an agent (commits, MCP calls).

### Lint

`scripts/test.py` runs it, or on its own:

```
uvx ruff check --config dev/ruff.toml src scripts tests
```

It checks PEP 8 style and common bugs. Files only for developing Mako live in `dev/`, such as
`dev/ruff.toml` with its rules and why some are turned off.

ruff doesn't catch library features newer than 3.8 (it missed `Path.write_text(newline=)`, which
needs 3.10), so also try changes under 3.8, for example in a `testing/` deployment with
`uv run --no-project --python 3.8 python ../../src/mako.py ...`.
