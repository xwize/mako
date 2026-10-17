#!/usr/bin/env bash
# Opens a tmux session with the orchestrator and the worker slots in split panes. UNTESTED.
# Lives at <root>/.mako/launchers/mako.tmux.launch.sh; mako.launch.sh runs it when tmux is the terminal found.
# Attaches when run in a terminal, else prints the attach command.
#
#   mako.tmux.launch.sh [resume]   resume: each agent continues its latest conversation

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
AGENT="$ROOT/.mako/agent.sh"
EXTRA=${1:+resume}
SLOTS=(orchestrator $(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["agents"]))' "$ROOT/.mako/config.json"))
PROJECT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$ROOT/.mako/config.json")"

# The shell command one pane runs (paths are shell-quoted).
pane_cmd() { printf 'bash %q %q %s' "$AGENT" "$1" "$EXTRA"; }
dir_of() { if [ "$1" = orchestrator ]; then echo "$ROOT"; else echo "$ROOT/.mako/worktrees/$1"; fi; }

SESSION="mako-$PROJECT"
if tmux has-session -t "$SESSION" 2>/dev/null; then echo "tmux session $SESSION already exists; attaching"
else
  first=1
  for s in "${SLOTS[@]}"; do
    dir="$(dir_of "$s")"
    if [ $first = 1 ]; then tmux new-session -d -s "$SESSION" -c "$dir" "$(pane_cmd "$s")"; first=0
    else tmux split-window -h -t "$SESSION" -c "$dir" "$(pane_cmd "$s")"; fi
    tmux select-pane -t "$SESSION" -T "$PROJECT [$s]"
    tmux select-layout -t "$SESSION" even-horizontal
  done
  tmux set-option -t "$SESSION" pane-border-status top
  tmux set-option -t "$SESSION" pane-border-format ' #{pane_title} '
  tmux select-pane -t "$SESSION:0.0"
fi
if [ -t 1 ]; then exec tmux attach -t "$SESSION"; else echo "attach with: tmux attach -t $SESSION"; fi
