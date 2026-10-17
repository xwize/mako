#!/usr/bin/env bash
# Opens Konsole with the orchestrator and the worker slots side by side in one split window
# (left to right: orchestrator, then each slot), like mako.launch.bat does in Windows Terminal.
# Lives at <root>/mako.konsole.launch.sh; mako.launch.sh runs it when Konsole is found.
#
#   mako.konsole.launch.sh [resume]   resume: each agent continues its latest conversation
#   MAKO_KONSOLE=tabs                 one tab per pane instead of a split

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
AGENT="$ROOT/.mako/agent.sh"
EXTRA=${1:+resume}
SLOTS=(orchestrator $(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["agents"]))' "$ROOT/.mako/config.json"))
PROJECT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$ROOT/.mako/config.json")"

# The shell command one pane runs (paths are shell-quoted).
pane_cmd() { printf 'bash %q %q %s' "$AGENT" "$1" "$EXTRA"; }
dir_of() { if [ "$1" = orchestrator ]; then echo "$ROOT"; else echo "$ROOT/.mako/worktrees/$1"; fi; }

konsole_services() { gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.ListNames 2>/dev/null | grep -o 'org\.kde\.konsole-[0-9]*' | sort -u; }

# Konsole's --layout JSON can split panes but ignores per-pane commands, while --tabs-from-file
# runs a command per tab. So: open the panes as tabs, then (unless MAKO_KONSOLE=tabs) move the
# tabs into one split over Konsole's D-Bus API (Window.moveView). Titles come from agent.sh.
# --tabs-from-file always adds a tab of its own, so the last slot is that tab (-e).
LAST="${SLOTS[${#SLOTS[@]}-1]}"
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
    python3 - "$NEW" <<'PYEOF'
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
rm -f "$TABS"
