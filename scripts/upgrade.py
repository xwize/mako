#!/usr/bin/env python3
"""Install the dev mako.py into a workspace's .mako/. Standard library only.

We develop Mako with Mako: the board runs on <workspace>/.mako/mako.py while
development happens in <workspace>/<project>/src/mako.py. This copies the
merged dev copy (the main checkout, never a worker's worktree) over the
installed one, keeping a timestamped backup in .mako/backups/. If the new
copy fails `mako --help`, the backup is put straight back.

It only replaces .mako/mako.py: kanban/, config.json, locks and worktrees are
left alone. Files that `init` generates (bin shims, agent.cmd, agents/*.mcp.json,
mako.launch.bat) are not regenerated.

    python upgrade.py                    upgrade the workspace this repo lives in
    python upgrade.py --workspace DIR    upgrade another workspace (or set MAKO_ROOT)
    python upgrade.py --source FILE      install a specific mako.py
    python upgrade.py --rollback         restore the newest backup
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


class UpgradeError(Exception):
    pass


def find_workspace(arg: str | None) -> Path:
    if arg:
        root = Path(arg).resolve()
    elif os.environ.get("MAKO_ROOT"):
        root = Path(os.environ["MAKO_ROOT"]).resolve()
    else:
        # Walk up from this script, not the current directory, so it works from anywhere.
        # From the main checkout or a slot worktree this reaches the workspace root.
        here = Path(__file__).resolve().parent
        root = next((d for d in here.parents if (d / ".mako" / "config.json").is_file()), None)
        if root is None:
            raise UpgradeError(f"no .mako/config.json above {here}; pass --workspace")
    if not (root / ".mako" / "config.json").is_file():
        raise UpgradeError(f"{root} is not a mako workspace (no .mako/config.json)")
    return root


def describe(path: Path) -> str:
    data = path.read_bytes()
    m = re.search(rb'^VERSION = "([^"]+)"', data, re.M)
    version = m.group(1).decode() if m else "?"
    mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return f"v{version}  sha256 {hashlib.sha256(data).hexdigest()[:12]}  {mtime}"


def git(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def check_source(src: Path, base: str, force: bool) -> list[str]:
    """Refuse uncommitted or off-branch dev code unless forced; return warnings."""
    problems: list[str] = []
    if git(src.parent, "rev-parse", "--is-inside-work-tree") != "true":
        return [f"warning: {src} is not in a git repo; cannot check it is merged code"]
    dirty = git(src.parent, "status", "--porcelain", "--", src.name)
    if dirty:
        problems.append(f"{src} has uncommitted changes")
    branch = git(src.parent, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != base:
        problems.append(f"{src.parent} is on '{branch}', not the base branch '{base}'")
    if problems and not force:
        raise UpgradeError("; ".join(problems) + " (commit/merge first, or pass --force)")
    return [f"warning: {p} (--force)" for p in problems]


def upgrade(root: Path, src: Path | None, force: bool, dry_run: bool) -> None:
    config = json.loads((root / ".mako" / "config.json").read_text(encoding="utf-8"))
    dest = root / ".mako" / "mako.py"
    src = src.resolve() if src else root / config["project"] / "src" / "mako.py"
    if not src.is_file():
        raise UpgradeError(f"source not found: {src}")
    if src == dest.resolve():
        raise UpgradeError("source and destination are the same file")

    for w in check_source(src, config.get("base_branch", "main"), force):
        print(w)

    # Never install code that does not even compile.
    with tempfile.TemporaryDirectory() as tmp:
        try:
            py_compile.compile(str(src), cfile=str(Path(tmp) / "mako.pyc"), doraise=True)
        except py_compile.PyCompileError as e:
            raise UpgradeError(f"source does not compile:\n{e.msg}")

    print(f"source:      {src}")
    print(f"             {describe(src)}")
    print(f"destination: {dest}")
    if dest.is_file():
        print(f"             {describe(dest)}")
        if dest.read_bytes() == src.read_bytes():
            print("Already up to date; nothing to do.")
            return
    else:
        print("             (not installed yet)")

    backup = None
    if dest.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = root / ".mako" / "backups" / f"mako.py.{stamp}.bak"
        n = 1
        while backup.exists():
            n += 1
            backup = backup.with_name(f"mako.py.{stamp}-{n}.bak")
    if dry_run:
        print(f"backup:      {backup or '(none)'}")
        print("Dry run; nothing changed.")
        return

    if backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, backup)
        print(f"backup:      {backup}")
    tmp = dest.with_name(".mako.py.upgrade")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    # Smoke test that does not touch the board: the CLI must at least start.
    r = subprocess.run([sys.executable, str(dest), "--help"], capture_output=True, text=True)
    if r.returncode != 0:
        if backup:
            shutil.copy2(backup, dest)
        raise UpgradeError(f"installed copy failed `mako --help`; "
                           f"{'restored the backup' if backup else 'left it in place'}:\n{r.stderr}")
    print(f"Installed:   {describe(dest)}")
    print_reminders(root)


def rollback(root: Path, dry_run: bool) -> None:
    dest = root / ".mako" / "mako.py"
    def order(p: Path) -> tuple[str, int]:
        # mako.py.<stamp>.bak, then mako.py.<stamp>-<n>.bak for same-second backups
        m = re.fullmatch(r"mako\.py\.(\d{8}-\d{6})(?:-(\d+))?\.bak", p.name)
        return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)
    backups = sorted((root / ".mako" / "backups").glob("mako.py.*.bak"), key=order)
    if not backups:
        raise UpgradeError("no backups in .mako/backups/")
    latest = backups[-1]
    print(f"restore:     {latest}")
    print(f"             {describe(latest)}")
    print(f"over:        {dest}")
    if dest.is_file():
        print(f"             {describe(dest)}")
    if dry_run:
        print("Dry run; nothing changed.")
        return
    tmp = dest.with_name(".mako.py.upgrade")
    shutil.copy2(latest, tmp)
    os.replace(tmp, dest)
    # Consume the backup so repeated rollbacks step further back.
    latest.unlink()
    print(f"Restored:    {describe(dest)}")
    print_reminders(root)


def print_reminders(root: Path) -> None:
    print()
    print("Next:")
    print(f"  - check it works: run `mako status` and `mako list` in {root}")
    print("  - running MCP servers keep the old code until their Claude sessions restart;")
    print("    restart the orchestrator and agent panes to pick up the new version")
    print("  - files generated by `mako init` (bin shims, agent.cmd, agents/*.mcp.json,")
    print("    mako.launch.bat) were not regenerated")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="upgrade", description="Install the dev mako.py into a workspace's .mako/.")
    p.add_argument("--workspace", help="workspace root (default: MAKO_ROOT, else the workspace above this script)")
    p.add_argument("--source", help="mako.py to install (default: <workspace>/<project>/src/mako.py)")
    p.add_argument("--force", action="store_true", help="install even with uncommitted changes or off the base branch")
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--rollback", action="store_true", help="restore the newest backup")
    a = p.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8")
    try:
        root = find_workspace(a.workspace)
        print(f"workspace:   {root}")
        if a.rollback:
            rollback(root, a.dry_run)
        else:
            upgrade(root, Path(a.source) if a.source else None, a.force, a.dry_run)
        return 0
    except UpgradeError as e:
        print(f"upgrade: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
