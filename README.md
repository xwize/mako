# Mako

Multi-Agent Kanban for Obsidian: a quick start for running several Claude Code agents on one project.
You get a kanban board you view and edit in Obsidian, a git repo, one permanent git worktree per agent
slot, issue claiming with locks, and an MCP server so agents can claim, work on and close issues
themselves.

![The Mako board in Obsidian next to the Windows Terminal agent panes](images/usage.png)

## Install

Open PowerShell in an empty folder that will become the workspace, and run:

```powershell
irm https://raw.githubusercontent.com/xwize/mako/main/install.ps1 | iex
```

On Linux, open a terminal in an empty folder and run:

```sh
curl -fsSL https://raw.githubusercontent.com/xwize/mako/main/install.sh | bash
```

Either one asks for a project name. To pass it (and other options) on the command line instead, use:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/xwize/mako/main/install.ps1))) -Project myproj
```

On Linux, pass options with `bash -s --` (or download `install.sh` and run it directly):

```sh
curl -fsSL https://raw.githubusercontent.com/xwize/mako/main/install.sh | bash -s -- --project myproj
```

`irm | iex` can't take arguments, which is why the second form exists. Neither form runs a script
file, so the execution policy doesn't block them and you don't need to change it. Both work in
Windows PowerShell 5.1 and PowerShell 7. On an old Windows PowerShell 5.1 that fails to download
with a TLS or "could not create SSL/TLS secure channel" error, run this first in the same window:

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 'Tls12'
```

### Options

PowerShell flags are on the left, the Linux `install.sh` equivalents on the right.

| Option | Meaning |
| --- | --- |
| `-Project <name>` / `--project <name>` | Project folder and git repo name. Issue ids use it as their prefix: `myproj` gives `MYPROJ-0001`. |
| `-Prefix <LETTERS>` / `--prefix <LETTERS>` | Issue id prefix, if you want a different one. |
| `-Agents a,b,c` / `--agents a,b,c` | Worker slot names (default `agent-a,agent-b`). |
| `-Base <branch>` / `--base <branch>` | Base branch (default `main`). |
| `-Ref <branch-or-tag>` / `--ref <branch-or-tag>` | Mako version to install (default `main`). Use a release tag such as `-Ref v0.1.0` to pin. |
| `-Repo <url-or-path>` / `--repo <url-or-path>` | Where to get Mako from (default this GitHub repo). A local clone works, for testing. |
| `-Upgrade` / `--upgrade` | The folder is already a workspace: install the downloaded Mako into it. |
| `-Force` / `--force` | Don't ask before setting up in a folder that isn't empty. |
| `--path` | Linux only. Also install `~/.local/bin/mako`, a shim that runs the Mako of the workspace you are in (it errors cleanly outside one). Put `~/.local/bin` on your PATH. |

The installer installs `main` by default. There are no release tags yet. Once there are, `-Ref`
pins to one.

### Prerequisites

- **Python 3.8 or newer** as `python` on PATH. Mako is standard-library Python only,
  and everything it generates runs `python`, so `py -3` alone isn't enough. A `python` that opens
  the Microsoft Store is only a stub. Install from [python.org](https://www.python.org/downloads/)
  and tick *Add python.exe to PATH*.
- **git** ([Git for Windows](https://git-scm.com/download/win)).
- **Linux**: Python 3 and git (2.30 or newer is best), from your package manager. Optional, for the launcher: `konsole`, `gnome-terminal` or `tmux`, and **Claude Code**. No root, no shell profile edits; use `./mako.sh` and `./mako.launch.sh` afterwards.
- Optional on Windows, needed by the launcher: **Windows Terminal** (`wt`) and **Claude Code** (`claude`). The
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

What each agent is told is built fresh from the files in `.mako/prompts/`. The `mako` MCP server's
instructions, which every agent gets when it starts, are the core: `common.txt` plus `worker.txt`
or `orchestrator.txt`. Claude Code cuts those instructions off at 4096 characters, so keep the
core short; the server warns when it isn't. The rest is in guide topics, read with the `guide`
tool's `topic` or `mako guide <topic>`; `mako guide` alone prints the core and every topic.

| File | Part |
| --- | --- |
| `common.txt` | core: the workspace and the rules, and the topics there are |
| `worker.txt` | core: a worker slot's role and how to finish an issue |
| `orchestrator.txt` | core: the orchestrator's role |
| `guide_review.txt` | topic `review`: the code review steps, for workers |
| `guide_review_<backend>.txt` | topic `review`: a review backend's own section (`guide_review_gerrit.txt`) |
| `guide_review_agents.txt` | topic `review`: how workers review each other's issues, when the review backend takes comments (Gerrit does) |
| `guide_hooks.txt` | topic `hooks`: the checks close runs, for workers (`{pre_close_commands}` lists them) |
| `guide_cli.txt` | topic `cli`: the CLI list |
| `claude-pointer.txt` | the line `mako init` puts at the top of a new workspace's `CLAUDE.md` |

They can use `{root}`, `{project}`, `{base}`, `{prefix}`, `{agent}`, `{agents}` and `{worktree}`;
other braces are left as they are. Older workspaces' `cli.txt`, `pre-close.txt` and `review*.txt`
are renamed by `mako init`, edits included. A deleted file shows a "missing" message in its place, and
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

Each pane's Claude session is named `<project>-<slot>-<tag>` (for example `myproj-agent-a-3f9c`), so
sessions of different Mako workspaces on one machine can't be mixed up when one session messages
another. The 4-hex `session_tag` is made by `mako init`, kept in `.mako/config.json` and never
changed; `mako session-name <slot>` prints a name.

## Your own tasks

Mark a card as yours with `mako new feature "Set up the API key" --human`, `mako edit <ID> --human`,
or by typing it on the board with a `#human` tag. Human cards are a plain kanban: drag them to any
column, with no locks, branches or merges, and agents can't claim or close them. An agent issue can
depend on one; it unblocks when you move it to Done.

## Approving issues a worker creates

Issues you or the orchestrator create can be claimed straight away. An issue a worker slot creates
starts with `approved: false` and shows `[unapproved]` in `mako list`; no agent can claim it until
you set `approved: true` in the card's properties.

## Merging into a feature branch

By default closing merges an issue into the base branch. Give a card a `merge_to` branch to merge it
there instead: `mako new feature "Login" --merge-to feature/auth`, or `mako edit <ID> --merge-to ..`
while it is in the Backlog (`''` clears). The branch is created from the base branch if it doesn't
exist. Workers still merge the base branch into their issue branch before closing, and the main
checkout is left on the base branch, so switch to `merge_to` yourself to try the result. Not for spikes.

## Spikes

A spike is research or a prototype: it never merges, and closing keeps its branch. Its result is
a summary file. When a worker claims a spike, Mako commits `.mako_spikes/spike_<ID>_<slug>.md` on
the spike's branch, with frontmatter (`id`, `title`, `agent`, `created`, `branch`, `closed`)
filled in. The worker writes the findings in its body and commits, and closing fills in `closed`.

To get the summary off the branch, give the spike a `publish_to` folder:
`mako new spike "Try caching" --publish-to docs/spikes`, or later with
`mako edit <ID> --publish-to ..`, which also works while the spike is In Progress. Closing then
copies the summary to `<publish_to>/spike_<ID>_<slug>.md`. A relative folder is taken from the
workspace root. It's a plain copy, never committed, and a re-close after `mako reopen` overwrites
it. A spike with `publish_to` but no summary on its branch can't be closed, except with
`mako close <ID> --force`.

`"spike_publish_to"` in `.mako/config.json` fills in `publish_to` on new spikes. Leave it empty
(`""`, the default) for none.

## Code review

Mako stops each issue for your review before it merges. When a worker's branch is ready, it
asks for review (`request_review`) and waits. Reply "approve" in its pane, or paste your comments
and it fixes them and asks again. Nothing merges until the latest commits have been reviewed.

By default the review is manual: the worker shows you the change in its pane (a summary, the
branch name and `git diff <base>...<branch> --stat`). To open a review tool instead, set
`review_hook` in `.mako/config.json`, for example to [difit](https://github.com/yoshiko-pg/difit):

```json
"review_hook": "npx difit HEAD {base}"
```

To let workers merge without your approval, set `"auto_approve": true` (default `false`). It only
applies when there is no `review_hook` and no `review` backend: a configured review tool is an
explicit choice, so `auto_approve` is ignored alongside one and each review request says so.
Spikes are never reviewed (they don't merge), and your own `mako close` isn't held up by it.

`review_hook` is one of Mako's [hooks](#hooks), so it can use all of their parameters. Its
output goes to `.mako/logs/<ID>.review.log`, and `mako review <ID>` reopens it. If it can't be
launched, the review request fails and no review is recorded.

### Reviewing in Gerrit (optional)

Instead of a review command, Mako can send each issue to a [Gerrit](https://www.gerritcodereview.com/)
you run yourself. You get patch sets, inline comments and votes, and agents read your comments
themselves instead of you pasting them. Add a `review` block to `.mako/config.json`. The backend is
then the review step, and a `review_hook` still runs after each successful upload, with the
change in `{review_ref}` and `{review_url}`:

```json
"review": {"backend": "gerrit", "url": "http://127.0.0.1:8080", "project": "demo"}
```

`url` defaults to `http://127.0.0.1:8080` and `project` to the workspace's project name.

Mako itself knows nothing about Gerrit. `"backend": "gerrit"` loads
`.mako/templates/review/gerrit.py`, and Mako calls its hooks: `setup`, `request` (for
`request_review`), `status` (for `review_status`), `comment` (for `post_review`) and `finish`
(after close, release or discard).
The hooks are described at the top of `mako_review.py`.

**You supply the Gerrit; Mako doesn't start, stop or install anything.** The reference setup is
trayit, a tray tool that runs the official `gerritcodereview/gerrit` image in Docker under WSL
on port 8080. Any Gerrit will do if it is:

- **in dev-auth mode** (`DEVELOPMENT_BECOME_ANY_ACCOUNT`, the official image's default). Mako
  creates its accounts by signing in as them, with no passwords to set up; setup refuses any
  other mode.
- **reachable only from your machine.** Dev auth lets anyone who can reach the port act as any
  account, administrator included, so bind it to `127.0.0.1` and never expose it on a network.

How it works:

- `mako review setup` creates the project, pushes the base branch and adds the accounts: `human`
  (you, an administrator) and one per agent slot. It is safe to re-run, and `request_review` runs
  it for you when needed, including after the Gerrit has been reset.
- Each issue is one change. Every `request_review` uploads the branch, squashed, as a new patch
  set. The reply gives two links: the change itself, which opens as whoever your browser is
  already signed in as, and a sign-in link, which uses dev auth to sign you in as `human` (no
  password) and then opens the change. Use the sign-in link so your comments and votes are `human`'s.
- When you've left comments, tell the agent. It calls `review_status` to read them, addresses
  them and asks for review again. You approve in the agent's pane by replying "approve".
- Gerrit never merges. Closing merges the branch as usual and abandons the change. Release and
  discard abandon the change too.
- To keep each change's history, set `"review_transcripts_to"` in `.mako/config.json` to a
  folder (relative to the workspace root, created if needed). Closing then
  saves it as `<folder>/review_<ID>.md`. It's empty by default, which keeps nothing.
- If the Gerrit is down, `request_review` says so; close, release and discard still work.

#### Agents reviewing each other

With a backend that takes comments (Gerrit does), you can ask one slot to review another's issue
before you do: "agent-b, review DEMO-0001", or "agent-b, review greet.py in DEMO-0001". The
reviewer calls `review_status`, reads the uploaded change from its own worktree (`git diff`,
`git show`; every worktree shares the repo's objects) and posts with `post_review`: a message,
inline comments and a score of -1, 0 or +1, as its own Gerrit account. Tell the author it's there;
it reads it with `review_status` like yours.

- Agent reviews are advisory. They never gate a close, an agent can't score +2, and only you
  approve. Agents treat each other's comments as suggestions and yours as must-fix.
- A slot can't review its own claim, and a review of an outdated upload (the author asked for
  review again since) is refused. Comments must be on files the change modifies, at lines that
  exist. A review of some files only (`files`) says so and can't score +1.
- Reviewing takes no claim: a slot can review while it holds its own issue.
- An agent's -1 is dropped by Gerrit on the next patch set, so it doesn't linger after a fix.
  A score of 0 is comments only and leaves the reviewer's earlier vote on that patch set as it is.
- `list_issues` (and `mako list`) marks an issue `[in review]` while its latest upload is waiting.
- From a terminal: `mako review <ID> --comment "msg" [--score N] [--inline path:line=text]..
  [--comments-file f.json] [--files a,b] [--sha S]`. It posts as `MAKO_AGENT` in an agent pane,
  else as `human`, who may score -2..2. `--comments-file` holds
  `[{"path": .., "line": .., "message": ..}]`; leave out `line` for a comment on the whole file.

The account tokens live in `.mako/review/gerrit/credentials.json`, and only Mako's tools call
Gerrit. Per-slot accounts give *attribution*, not *enforcement*: an agent that read that file could
act as any account.

## Hooks

Hooks run your own commands at points in an issue's life. Set them in `.mako/config.json`:

| Key | Runs | Working folder |
| --- | --- | --- |
| `new_hook` | after an issue is created: `mako new`, `create_issue`, or a card you typed on the board | the workspace |
| `claim_hook` | after a claim (`mako claim`, `claim_issue`, `claim_next`) has checked out the branch | the slot's worktree |
| `review_hook` | on every review request (`mako review <ID>`, `request_review`) | the slot's worktree |
| `pre_close_hook` | on close, before the merge; a failure refuses the close (see [Checks before close](#checks-before-close)) | the slot's worktree, on the branch |
| `close_hook` | after a successful close, once the merge is done; not when the close fails | the main checkout |

Each one is a command or a list of commands. Every command in a list is started in order,
detached, without waiting for the one before, so a hook never slows Mako down. The output goes to
`.mako/logs/<ID>.<new|claim|review|close>.log`, with a header (time, folder, the command as run)
for each command. A hook that can't be launched only prints a warning and the Mako command still
succeeds. The exception is `review_hook` as the review step (no `review` backend set), which then
fails the request. `pre_close_hook` is different again: see below.

### Parameters

A command can use these placeholders, which Mako fills in before launching it:

| Parameter | Value |
| --- | --- |
| `{id}`, `{title}`, `{type}` | the issue |
| `{base}`, `{branch}`, `{sha}` | the base branch, the issue branch, the branch's head commit |
| `{agent}` | the slot that holds the claim |
| `{root}` | the workspace folder (the Obsidian vault, with `kanban/` and `.mako/`) |
| `{repo}` | the main checkout (`<workspace>/<project>`) |
| `{worktree}` | the claiming slot's worktree (`.mako/worktrees/<slot>`) |
| `{review_ref}`, `{review_url}` | `review_hook` only, when the review backend returns them (Gerrit: the change number and its URL) |

- A parameter with no value yet is empty: `new_hook` has no branch, sha, agent or worktree.
- Values go in as they are. Quote the ones that can hold spaces: `"{worktree}"`.
- `{{` and `}}` are a literal `{` and `}`.
- An unknown `{name}` is left as it is, with a warning.

The same values are in the hook's environment as `MAKO_<NAME>` (`MAKO_ID`, `MAKO_BRANCH`,
`MAKO_WORKTREE`, ...), plus `MAKO_EVENT` (`new`, `claim`, `review`, `pre_close` or `close`), so a hook can be a
script (`.ps1`, `.cmd`, `.sh`) that reads them and needs no quoting. The workspace folder is
`MAKO_WORKSPACE`, not `MAKO_ROOT`: Mako uses `MAKO_ROOT` to find its workspace, so it is never set
for a hook.

### Your own parameters

`hook_params` names things once for every hook. Each entry adds a `{name}` placeholder and a
`MAKO_PARAM_<NAME>` variable. A value can use the built-in placeholders (expanded once). The
built-in names can't be redefined; Mako warns and ignores them.

```json
"hook_params": {"ide": "code", "gerrit": "http://127.0.0.1:8080"},
"new_hook":    "{ide} \"{root}\"",
"claim_hook":  "{ide} \"{worktree}\"",
"review_hook": ["{ide} \"{worktree}\"", "start \"\" \"{review_url}\""]
```

Here a new issue opens the IDE on the workspace and a claim opens it on the slot's worktree. With
the Gerrit backend, a review request opens the worktree in the IDE and the change in the browser
at the same time (`start` is cmd's; on Linux use `xdg-open`, on macOS `open`).

### Checks before close

`pre_close_hook` holds the checks that must pass before an issue merges, such as a full build and
the unit tests, so neither an agent nor you can forget them:

```json
"pre_close_hook": ["cmake --build build --config Release", "ctest --test-dir build"],
"pre_close_timeout": 1800
```

On close (`close_issue`, `mako close`), after Mako's own checks (owner, clean worktree, review,
branch not behind the base branch) and before the merge, Mako runs the commands one at a time in
the slot's worktree, on the issue branch, and waits for each. That branch already contains the
base branch, so the checks see what will land. If the issue isn't claimed, they run in a
temporary worktree at the branch (`.mako/tmp/`), removed afterwards. The first command that exits
non-zero stops the rest and refuses the close: nothing changes, the card stays In Progress and the
claim is kept. The error names the command, its exit code and the log, and shows the end of its
output. The output is in `.mako/logs/<ID>.pre_close.log`.

- They take the same parameters, `MAKO_*` variables and `hook_params` as the other hooks.
- They are skipped for spikes (which never merge) and when the branch has nothing to merge.
- `pre_close_timeout` (seconds, `0` or unset: no limit) kills a command that runs longer, with
  everything it started, and refuses the close.
- The checks run without holding the board, so other commands carry on meanwhile. A commit made
  to the branch while they run makes the close refuse; close again.
- `mako close --force`, run by you, skips them with a warning. Agents can't: `close_issue` has no
  way to, and `mako close --force` from a worker slot still runs them.
- Workers' guides get a short section (`.mako/prompts/guide_hooks.txt`, guide topic `hooks`) listing the checks.
- Build output in the worktree must be gitignored: checks that leave the worktree unclean refuse
  the close.

## Reminding you to /clear

A long conversation costs tokens on every turn. When a worker closes an issue that was open for
more than `warn_after_minutes` (default 30) or reviewed more than `warn_after_reviews` times
(default 2), its next claim is refused once, and it asks you to run `/clear` or `/compact` in its
pane and then ask again. It is a nudge: the retry always works, cleared or not. Set a value to `0`
to turn that test off; both `0` means no reminders. The flag lives in `.mako/warn/<slot>`.

## Keeping issue branches

By default, closing an issue merges its branch into the base branch and deletes it. To keep the
branches, set `keep_branches` in `.mako/config.json`:

```json
"keep_branches": true
```

Closing then keeps the local branch and names it in the merge commit, for example
`Merge feature/MYPROJ-0005-login (MYPROJ-0005): Login`. Reopening and claiming such an issue
resumes its old branch; the worker merges the base branch into it before closing again.

`mako prune` deletes the kept branches of Done issues, whatever the setting. It uses
`git branch -d`, so only fully merged branches go; a spike's unmerged branch is skipped. It never
touches a Backlog or In Progress issue's branch, the base branch or a branch checked out in a
worktree.

- `mako prune --dry-run` lists what would be deleted.
- `mako prune <ID..>` prunes only those issues.
- `mako prune --discarded` also deletes the branches of discarded issues (`git branch -D`). Their
  work was never merged, so `--dry-run` shows how many commits each would lose.

## Development

Mako's code is `src/mako.py`, the entry point, plus the `src/mako_*.py` modules it imports
(standard library only; the module list is in `mako.py`'s docstring), and `scripts/upgrade.py`. It
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
