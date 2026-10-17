#!/usr/bin/env bash
# Mako installer for Linux/macOS: makes the current folder a Mako workspace.
#
#   curl -fsSL https://raw.githubusercontent.com/xwize/mako/main/install.sh | bash -s -- --project myproj
#   ./install.sh --project myproj
#
# Options:
#   --project <name>   project folder / git repo name (asked for if missing and a terminal is available)
#   --ref <ref>        branch or tag of Mako to install (default: main)
#   --repo <url|path>  where to get Mako from (default: the GitHub repo; a local clone works too)
#   --prefix, --agents, --base   passed to `mako init`
#   --upgrade          the folder is already a workspace: install the downloaded Mako into it
#   --force            don't ask before setting up in a folder that isn't empty
#   --path             also put a `mako` command in ~/.local/bin (finds the workspace from where you are)
#
# Needs Python 3 and git (git 2.30+ for `git worktree repair`). No root, no shell rc edits:
# everything goes into .mako/ in this folder, plus ~/.local/bin/mako with --path.
# The download is a shallow clone into a temp folder, deleted afterwards.

set -u
PROJECT="" REF=main REPO="https://github.com/xwize/mako.git" PREFIX="" AGENTS="" BASE="" UPGRADE=0 FORCE=0 LINKPATH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;  --ref) REF="${2:-}"; shift 2 ;;  --repo) REPO="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;    --agents) AGENTS="${2:-}"; shift 2 ;;  --base) BASE="${2:-}"; shift 2 ;;
    --upgrade) UPGRADE=1; shift ;;           --force) FORCE=1; shift ;;             --path) LINKPATH=1; shift ;;
    -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "mako install: unknown option $1" >&2; exit 1 ;;
  esac
done

fail() { echo "mako install: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }
has() { command -v "$1" >/dev/null 2>&1; }
# Reading answers works under `curl | bash` too, because the questions come from the terminal, not stdin.
ask() { REPLY=""; { read -r -p "$1" REPLY < /dev/tty; } 2>/dev/null || REPLY=""; }

echo "Mako installer"
ROOT="$PWD"

# --- prerequisites -----------------------------------------------------------
PY=""
for c in python3 python; do
  if has "$c" && [ "$("$c" -c 'import sys; print(sys.version_info[0])' 2>/dev/null)" = 3 ]; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "Python 3 not found. Install it with your package manager (e.g. sudo apt install python3, sudo pacman -S python)."
echo "  python   $("$PY" --version 2>&1)"
has git || fail "git not found. Install it with your package manager (e.g. sudo apt install git, sudo pacman -S git)."
echo "  git      $(git --version)"
GITV="$(git --version | sed 's/[^0-9.]*\([0-9][0-9.]*\).*/\1/')"
[ "$(printf '%s\n2.30\n' "$GITV" | sort -V | head -1)" = 2.30 ] || warn "git $GITV is older than 2.30 and has no 'git worktree repair'; moving a workspace between Windows and Linux will not self-repair"
TERMS=""; for t in konsole gnome-terminal tmux; do has "$t" && TERMS="$TERMS $t"; done
if [ -n "$TERMS" ]; then echo "  terminal$TERMS"
else warn "no supported terminal found (konsole, gnome-terminal, tmux); mako.launch.sh will print the commands to run by hand"; fi
has claude && echo "  claude   found" || warn "Claude Code (claude) not found; the agent panes need it. See https://claude.com/claude-code"

# --- what to do here -----------------------------------------------------------
IS_WS=0; [ -f "$ROOT/.mako/config.json" ] && IS_WS=1
if [ $IS_WS = 1 ] && [ $UPGRADE = 0 ]; then
  fail "$ROOT is already a Mako workspace (.mako/config.json exists); nothing changed.
  To install the latest Mako into it, re-run with --upgrade."
fi
[ $UPGRADE = 0 ] || [ $IS_WS = 1 ] || fail "--upgrade: $ROOT is not a Mako workspace (no .mako/config.json)."
if [ $IS_WS = 0 ]; then
  if [ -z "$PROJECT" ]; then ask "Project name (folder for the git repo): "; PROJECT="$REPLY"; fi
  [[ "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "bad project name '$PROJECT' (letters, digits, '.', '_', '-')"
  OTHERS="$(find "$ROOT" -mindepth 1 -maxdepth 1 ! -name "$PROJECT" | wc -l)"
  if [ "$OTHERS" -gt 0 ] && [ $FORCE = 0 ]; then
    echo "$ROOT is not empty ($OTHERS items)."
    ask "Set up a Mako workspace here anyway? [y/N] "
    [[ "$REPLY" =~ ^([yY]|[yY][eE][sS])$ ]] || fail "cancelled; nothing changed."
  fi
fi

# --- get Mako -------------------------------------------------------------------
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mako-install.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading Mako ($REF) from $REPO"
git -c advice.detachedHead=false clone -q --depth 1 --branch "$REF" "$REPO" "$TMP/mako" 2>&1 || fail "git clone failed."
[ -f "$TMP/mako/src/mako.py" ] || fail "no src/mako.py in $REPO at $REF"

# --- mako init ---------------------------------------------------------------------
# init copies mako.py and its templates into .mako/, so the temp clone is not needed afterwards.
INIT=("$TMP/mako/src/mako.py" init)
if [ $IS_WS = 0 ]; then
  INIT+=("$PROJECT")
  [ -z "$PREFIX" ] || INIT+=(--prefix "$PREFIX")
  [ -z "$AGENTS" ] || INIT+=(--agents "$AGENTS")
  [ -z "$BASE" ] || INIT+=(--base "$BASE")
fi
"$PY" "${INIT[@]}" || fail "mako init failed (see above)."

# --- optional: a `mako` command on PATH ---------------------------------------------
if [ $LINKPATH = 1 ]; then
  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/mako" <<'SHIM'
#!/bin/sh
# Runs the Mako of the workspace you are in (walks up from the current folder to .mako/mako.py).
d="$PWD"
while [ "$d" != / ]; do
  if [ -f "$d/.mako/mako.py" ]; then exec "$(command -v python3 || command -v python)" "$d/.mako/mako.py" "$@"; fi
  d="$(dirname "$d")"
done
echo "mako: not inside a mako workspace (no .mako/mako.py found above $PWD)" >&2; exit 1
SHIM
  chmod +x "$HOME/.local/bin/mako"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "$HOME/.local/bin is not on your PATH; add it to your shell profile to use 'mako'" ;; esac
fi

echo
echo "Next steps:"
echo "  1. Open this folder as a vault in Obsidian (Open folder as vault) to see kanban/board.md."
echo "     Trust the vault when asked, so the Kanban plugin can load."
echo "  2. Make a first issue:   ./mako.sh new feature \"My first feature\""
echo "  3. Start the agents:     ./mako.launch.sh"
echo "     (orchestrator on the left, one Claude pane per worker slot)"
