#!/usr/bin/env bash
# Starts Claude for one mako slot:  agent.sh <slot|orchestrator> [resume]
# Linux counterpart of .mako/agent.cmd. Lives at <root>/.mako/agent.sh.
# "orchestrator" runs at the workspace root; worker slots run in their worktree.
# With "resume", Claude continues the slot's latest conversation (claude -c).

SLOT="$1"
[ -n "$SLOT" ] || { echo "usage: agent.sh <slot|orchestrator> [resume]"; exit 1; }
MAKODIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(dirname "$MAKODIR")"
PY="$(command -v python3 || command -v python)" || { echo "Python 3 not found"; exit 1; }
PROJECT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$MAKODIR/config.json")"

export MAKO_AGENT="$SLOT"
export PATH="$MAKODIR/bin:$PATH"

if [ "$SLOT" = orchestrator ]; then
  DIR="$ROOT"; ADD="$ROOT/$PROJECT"
else
  DIR="$MAKODIR/worktrees/$SLOT"; ADD="$ROOT"
fi
cd "$DIR" || { echo "no directory $DIR"; exec "${SHELL:-bash}"; }
# A worker slot whose worktree link is broken (workspace last used on another OS): warn, never repair.
if [ "$SLOT" != orchestrator ] && ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "WARNING: $SLOT's worktree is broken, probably from another OS. Run \"mako repair\" in the orchestrator pane."
fi

# Tab/pane title. OSC 0 sets icon+window title, OSC 30 is Konsole's tab title.
TITLE="$PROJECT [$SLOT]"
printf '\033]0;%s\007\033]30;%s\007' "$TITLE" "$TITLE"

ARGS=()
if [ "$2" = resume ]; then
  if "$PY" "$MAKODIR/mako.py" session >/dev/null 2>&1; then ARGS+=(-c)
  else echo "No earlier conversation for $SLOT; starting a new one."; fi
fi
# Name the session <project>-<slot>-<tag>; Claude drops the name on -c, so -n goes on every launch.
NAME="$("$PY" "$MAKODIR/mako.py" session-name "$SLOT" 2>/dev/null)"
[ -n "$NAME" ] && ARGS+=(-n "$NAME")

# MCP config is built here, not read from agents/<slot>.mcp.json: those files hold
# the absolute path of whichever OS ran `mako init`, which is wrong after switching OS.
MCP="$("$PY" -c 'import json,sys; print(json.dumps({"mcpServers":{"mako":{"command":sys.argv[1],"args":[sys.argv[2],"mcp"],"env":{"MAKO_AGENT":sys.argv[3]}}}}))' "$PY" "$MAKODIR/mako.py" "$SLOT")"

command -v claude >/dev/null || { echo "claude not found on PATH"; exec "${SHELL:-bash}"; }
claude "${ARGS[@]}" --add-dir "$ADD" --mcp-config "$MCP" --allowedTools mcp__mako
# Keep the pane open after Claude exits so output stays readable.
exec "${SHELL:-bash}"
