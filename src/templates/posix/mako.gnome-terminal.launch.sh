#!/usr/bin/env bash
# Opens GNOME Terminal with the orchestrator and the worker slots, one tab each (it has no
# split panes). UNTESTED. Lives at <root>/mako.gnome-terminal.launch.sh; mako.launch.sh runs
# it when gnome-terminal is found.
#
#   mako.gnome-terminal.launch.sh [resume]   resume: each agent continues its latest conversation

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
AGENT="$ROOT/.mako/agent.sh"
EXTRA=${1:+resume}
SLOTS=(orchestrator $(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["agents"]))' "$ROOT/.mako/config.json"))
PROJECT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$ROOT/.mako/config.json")"

# The shell command one pane runs (paths are shell-quoted).
pane_cmd() { printf 'bash %q %q %s' "$AGENT" "$1" "$EXTRA"; }
dir_of() { if [ "$1" = orchestrator ]; then echo "$ROOT"; else echo "$ROOT/.mako/worktrees/$1"; fi; }

# UNTESTED (not installed on the dev machine). Everything after `--` is the command, so a
# tab per call: the first call opens a window, later ones add tabs to it. --tab goes to the
# most recently active window, so don't click into another Terminal window while this runs.
first=1
for s in "${SLOTS[@]}"; do
  dir="$(dir_of "$s")"
  if [ $first = 1 ]; then mode=--window; first=0; else mode=--tab; fi
  gnome-terminal "$mode" "--title=$PROJECT [$s]" "--working-directory=$dir" -- bash "$AGENT" "$s" ${EXTRA:+"$EXTRA"} >/dev/null 2>&1
  sleep 0.7
done
