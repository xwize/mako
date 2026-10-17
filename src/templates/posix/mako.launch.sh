#!/usr/bin/env bash
# Opens a terminal with the orchestrator and the worker slots side by side
# (left to right: orchestrator, then each slot), like mako.launch.bat does in
# Windows Terminal.  Lives at <root>/mako.launch.sh.
#
#   mako.launch.sh [resume]        resume: each agent continues its latest conversation
#   MAKO_TERMINAL=konsole|gnome-terminal|tmux  force a terminal (default: first found, in that order)
#
# konsole         one window, real split panes (MAKO_KONSOLE=tabs for one tab per pane instead)
# gnome-terminal  one window, one tab per pane (it has no split panes; untested)
# tmux            a session with split panes; attaches if run in a terminal, else prints the command

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
AGENT="$ROOT/.mako/agent.sh"
PY="$(command -v python3 || command -v python)" || { echo "Python 3 not found" >&2; exit 1; }
EXTRA=${1:+resume}
SLOTS=(orchestrator $("$PY" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["agents"]))' "$ROOT/.mako/config.json"))
PROJECT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$ROOT/.mako/config.json")"

konsole_services() { gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.ListNames 2>/dev/null | grep -o 'org\.kde\.konsole-[0-9]*' | sort -u; }

# The shell command one pane runs (paths are shell-quoted).
pane_cmd() { printf 'bash %q %q %s' "$AGENT" "$1" "$EXTRA"; }

pick() {
  [ -n "$MAKO_TERMINAL" ] && { echo "$MAKO_TERMINAL"; return; }
  for t in konsole gnome-terminal tmux; do command -v "$t" >/dev/null && { echo "$t"; return; }; done
}
TERM_KIND="$(pick)"

case "$TERM_KIND" in
konsole)
  # Konsole's --layout JSON can split panes but ignores per-pane commands, while --tabs-from-file
  # runs a command per tab. So: open the panes as tabs, then (unless MAKO_KONSOLE=tabs) move the
  # tabs into one split over Konsole's D-Bus API (Window.moveView). Titles come from agent.sh.
  # --tabs-from-file always adds a tab of its own, so the last slot is that tab (-e).
  LAST="${SLOTS[${#SLOTS[@]}-1]}"
  dir_of() { if [ "$1" = orchestrator ]; then echo "$ROOT"; else echo "$ROOT/.mako/worktrees/$1"; fi; }
  TABS="$(mktemp --suffix=.mako-tabs)"
  for s in "${SLOTS[@]::${#SLOTS[@]}-1}"; do
    echo "title: $PROJECT [$s];; workdir: $(dir_of "$s");; command: $(pane_cmd "$s")" >> "$TABS"
  done
  BEFORE="$(konsole_services)"
  # --separate: its own process, so closing it never touches the user's other Konsole windows.
  konsole --separate --tabs-from-file "$TABS" --workdir "$(dir_of "$LAST")" \
    -p "LocalTabTitleFormat=$PROJECT [$LAST]" \
    -e bash "$AGENT" "$LAST" ${EXTRA:+"$EXTRA"} >/dev/null 2>&1 &
  if [ "${MAKO_KONSOLE:-split}" = split ] && command -v gdbus >/dev/null; then
    NEW=""
    for _ in $(seq 40); do  # the new instance's D-Bus name, org.kde.konsole-<pid>, appears once it is up
      NEW="$(comm -13 <(echo "$BEFORE") <(konsole_services) | head -1)"
      [ -n "$NEW" ] && [ "$(gdbus call --session --dest "$NEW" --object-path /Windows/1 \
        --method org.kde.konsole.Window.sessionCount 2>/dev/null)" = "(${#SLOTS[@]},)" ] && break
      NEW=""; sleep 0.25
    done
    if [ -n "$NEW" ]; then
      "$PY" - "$NEW" <<'PYEOF'
import re, subprocess, sys
dest = sys.argv[1]
def call(method, *args):
    return subprocess.run(["gdbus", "call", "--session", "--dest", dest, "--object-path", "/Windows/1",
                           "--method", "org.kde.konsole.Window." + method, *map(str, args)],
                          capture_output=True, text=True).stdout
# viewHierarchy looks like "('(0)[0]\n(1)[1]',): one "(splitter)[view|view]" per tab.
views = [int(v) for _, body in re.findall(r"\((\d+)\)\[([\d|]*)\]", call("viewHierarchy")) for v in body.split("|") if v]
for i, v in enumerate(views[1:], 1):
    call("moveView", v, 0, i)  # into the first tab's splitter, left to right in tab order
PYEOF
    else
      echo "mako: couldn't reach Konsole over D-Bus; left the panes as tabs" >&2
    fi
  fi
  rm -f "$TABS" ;;
gnome-terminal)
  # UNTESTED (not installed on the dev machine). Everything after `--` is the command, so a
  # tab per call: the first call opens a window, later ones add tabs to it. --tab goes to the
  # most recently active window, so don't click into another Terminal window while this runs.
  first=1
  for s in "${SLOTS[@]}"; do
    dir="$ROOT"; [ "$s" = orchestrator ] || dir="$ROOT/.mako/worktrees/$s"
    if [ $first = 1 ]; then mode=--window; first=0; else mode=--tab; fi
    gnome-terminal "$mode" "--title=$PROJECT [$s]" "--working-directory=$dir" -- bash "$AGENT" "$s" ${EXTRA:+"$EXTRA"} >/dev/null 2>&1
    sleep 0.7
  done ;;
tmux)
  SESSION="mako-$PROJECT"
  if tmux has-session -t "$SESSION" 2>/dev/null; then echo "tmux session $SESSION already exists; attaching"
  else
    first=1
    for s in "${SLOTS[@]}"; do
      dir="$ROOT"; [ "$s" = orchestrator ] || dir="$ROOT/.mako/worktrees/$s"
      if [ $first = 1 ]; then tmux new-session -d -s "$SESSION" -c "$dir" "$(pane_cmd "$s")"; first=0
      else tmux split-window -h -t "$SESSION" -c "$dir" "$(pane_cmd "$s")"; fi
      tmux select-pane -t "$SESSION" -T "$PROJECT [$s]"
      tmux select-layout -t "$SESSION" even-horizontal
    done
    tmux set-option -t "$SESSION" pane-border-status top
    tmux set-option -t "$SESSION" pane-border-format ' #{pane_title} '
    tmux select-pane -t "$SESSION:0.0"
  fi
  if [ -t 1 ]; then exec tmux attach -t "$SESSION"; else echo "attach with: tmux attach -t $SESSION"; fi ;;
*)
  echo "No supported terminal found (konsole, gnome-terminal, tmux). Run these in separate terminals:" >&2
  for s in "${SLOTS[@]}"; do echo "  $(pane_cmd "$s")" >&2; done
  exit 1 ;;
esac
