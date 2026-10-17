#!/usr/bin/env bash
# Opens the orchestrator and the worker slots in a terminal, like mako.launch.bat does in
# Windows Terminal. Lives at <root>/mako.launch.sh; the terminal-specific work is in
# .mako/launchers/mako.<terminal>.launch.sh.
#
#   mako.launch.sh [resume]        resume: each agent continues its latest conversation
#   MAKO_TERMINAL=konsole|gnome-terminal|tmux  force a terminal (default: first found, in that order)

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

TERM_KIND="$MAKO_TERMINAL"
if [ -z "$TERM_KIND" ]; then
  for t in konsole gnome-terminal tmux; do command -v "$t" >/dev/null && { TERM_KIND="$t"; break; }; done
fi

LAUNCHER="$ROOT/.mako/launchers/mako.$TERM_KIND.launch.sh"
if [ -f "$LAUNCHER" ]; then
  exec bash "$LAUNCHER" "$@"
fi
echo "No supported terminal found (konsole, gnome-terminal, tmux). Run each slot in its own terminal:" >&2
echo "  bash $ROOT/.mako/agent.sh orchestrator   (and agent.sh <slot> for each slot in .mako/config.json)" >&2
exit 1
