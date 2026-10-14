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

- **Python 3** as `python` on PATH. Mako is a single standard-library Python file, and everything it
  generates runs `python`, so `py -3` alone isn't enough. A `python` that opens the Microsoft
  Store is only a stub. Install from [python.org](https://www.python.org/downloads/) and tick
  *Add python.exe to PATH*.
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
```

### Next steps

1. Open the folder as a vault in Obsidian (*Open folder as vault*) and trust it, so the Kanban plugin
   loads. Open `kanban/board.md`.
2. Make a first issue: `.\mako.cmd new feature "My first feature"`.
3. Run `.\mako.launch.bat`. The orchestrator opens on the left and the worker slots on the right.
   Ask a worker to "pick up MYPROJ-0001".
4. Closed the terminal? `.\mako.cmd resume` reopens the same layout with every pane continuing its
   last Claude conversation (`claude -c`); a pane with no earlier conversation starts fresh.
   `mako resume <slot|orchestrator>` reopens just one tab.
