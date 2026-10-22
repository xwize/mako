#!/usr/bin/env python3
"""Install the dev Mako into a workspace's .mako/. Standard library only.

We develop Mako with Mako: the board runs on <workspace>/.mako/mako.py while
development happens in <workspace>/<project>/src/mako.py. This installs the
merged dev copy (the main checkout, never a worker's worktree) into a workspace.
Mako's code is mako.py, the entry point, plus the mako_*.py modules next to it;
everything here handles them as one set.

The install itself is the source's own `mako init`, run in the workspace: it
copies the code and templates/ into .mako/ and regenerates the files init owns
(bin shims, agent.cmd, agents/*.mcp.json, mako.launch.bat, .obsidian plugins).
Init never touches cards, config.json, locks or the project's history. What this
script adds around it are the safety checks: the source must be committed on
the base branch, compile and start (`--help`), and the old code and
templates/ are backed up to .mako/backups/<stamp>/ first. If init fails, the
backup is put straight back.

(Why not just document `python <project>/src/mako.py init`? It installs
whatever is in the file, uncommitted or not, with no way back. Those checks
belong to developing Mako, not to init, which end users run too.)

    python upgrade.py                    upgrade the workspace this repo lives in
    python upgrade.py --workspace DIR    upgrade another workspace (or set MAKO_ROOT)
    python upgrade.py --source FILE      install a specific mako.py (its modules and templates/)
    python upgrade.py --rollback         restore the newest backup and regenerate
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


# Mako's code: mako.py and its modules (as in src/mako_init.py).
CODE_FILES = "mako*.py"


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


def tree(path: Path) -> dict[str, bytes]:
    """Relative path -> content for every file under path (empty if missing)."""
    if not path.is_dir():
        return {}
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def code_paths(entry: Path) -> dict[str, Path]:
    """Mako's code files by name: mako.py and the mako_*.py modules next to it. An old-style
    backup file (mako.py.<stamp>.bak, from the single-file days) is mako.py alone."""
    if entry.name != "mako.py":
        return {"mako.py": entry}
    return {p.name: p for p in sorted(entry.parent.glob(CODE_FILES)) if p.is_file()}


def code(entry: Path) -> dict[str, bytes]:
    return {name: p.read_bytes() for name, p in code_paths(entry).items()}


def describe(entry: Path, templates: Path | None = None) -> str:
    paths = code_paths(entry)
    files = {name: p.read_bytes() for name, p in paths.items()}
    found = (re.search(rb'^VERSION = "([^"]+)"', data, re.M) for data in files.values())
    version = next((v.group(1).decode() for v in found if v), "?")
    newest = max(p.stat().st_mtime for p in paths.values())
    mtime = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")
    h = hashlib.sha256()
    for name, content in files.items():
        h.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    text = f"v{version}  sha256 {h.hexdigest()[:12]} ({len(files)} code files)  {mtime}"
    if templates is not None:
        files = tree(templates)
        h = hashlib.sha256()
        for rel, content in files.items():
            h.update(rel.encode() + b"\0" + hashlib.sha256(content).digest())
        text += f"  templates {h.hexdigest()[:12]} ({len(files)} files)" if files else "  no templates"
    return text


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
    dirty = git(src.parent, "status", "--porcelain", "--", CODE_FILES, "templates")
    if dirty:
        problems.append(f"{src.parent} has uncommitted changes to {CODE_FILES} or templates/")
    branch = git(src.parent, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != base:
        problems.append(f"{src.parent} is on '{branch}', not the base branch '{base}'")
    if problems and not force:
        raise UpgradeError("; ".join(problems) + " (commit/merge first, or pass --force)")
    return [f"warning: {p} (--force)" for p in problems]


def run_init(root: Path, script: Path) -> subprocess.CompletedProcess:
    """`<script> init` in the workspace: installs script + its templates, regenerates files."""
    return subprocess.run([sys.executable, str(script), "init"], cwd=root, check=False,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def restore(backup: Path, dot_mako: Path) -> None:
    """Put a backup's code (and templates/, if it has them) back into .mako/. Code files the
    backup doesn't have are removed, so a single-file backup leaves no newer modules behind."""
    saved = code_paths(backup / "mako.py" if backup.is_dir() else backup)
    for name, path in saved.items():
        tmp = dot_mako / f".{name}.upgrade"
        shutil.copy2(path, tmp)
        os.replace(tmp, dot_mako / name)
    for path in dot_mako.glob(CODE_FILES):
        if path.name not in saved:
            path.unlink()
    if backup.is_dir():
        shutil.rmtree(dot_mako / "templates", ignore_errors=True)
        if (backup / "templates").is_dir():
            shutil.copytree(backup / "templates", dot_mako / "templates")


def upgrade(root: Path, src: Path | None, force: bool, dry_run: bool) -> None:
    config = json.loads((root / ".mako" / "config.json").read_text(encoding="utf-8"))
    dot_mako = root / ".mako"
    dest, dest_tpl = dot_mako / "mako.py", dot_mako / "templates"
    src = src.resolve() if src else root / config["project"] / "src" / "mako.py"
    src_tpl = src.parent / "templates"
    if not src.is_file():
        raise UpgradeError(f"source not found: {src}")
    if src == dest.resolve():
        raise UpgradeError("source and destination are the same file")

    for w in check_source(src, config.get("base_branch", "main"), force):
        print(w)
    if not src_tpl.is_dir():
        print(f"warning: no templates/ next to {src}; .mako/templates will be left as it is")

    # Never install code that does not even compile, or start.
    with tempfile.TemporaryDirectory() as tmp:
        for name, path in code_paths(src).items():
            try:
                py_compile.compile(str(path), cfile=str(Path(tmp) / f"{name}c"), doraise=True)
            except py_compile.PyCompileError as e:
                raise UpgradeError(f"source does not compile:\n{e.msg}") from None
    r = subprocess.run([sys.executable, str(src), "--help"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise UpgradeError(f"source fails `mako --help`:\n{r.stderr}")

    print(f"source:      {src}")
    print(f"             {describe(src, src_tpl)}")
    print(f"destination: {dest}")
    if dest.is_file():
        print(f"             {describe(dest, dest_tpl)}")
        if code(dest) == code(src) and (not src_tpl.is_dir() or tree(dest_tpl) == tree(src_tpl)):
            print("Already up to date; nothing to do. (Run `mako init` to just regenerate files.)")
            return
    else:
        print("             (not installed yet)")

    backup = None
    if dest.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = dot_mako / "backups" / stamp
        n = 1
        while backup.exists():
            n += 1
            backup = backup.with_name(f"{stamp}-{n}")
    if dry_run:
        print(f"backup:      {backup or '(none)'}")
        print(f"then:        {src.name} init in {root}")
        print("Dry run; nothing changed.")
        return

    if backup:
        backup.mkdir(parents=True)
        for name, path in code_paths(dest).items():
            shutil.copy2(path, backup / name)
        if dest_tpl.is_dir():
            shutil.copytree(dest_tpl, backup / "templates")
        print(f"backup:      {backup}")
    r = run_init(root, src)
    if r.returncode != 0:
        if backup:
            restore(backup, dot_mako)
        raise UpgradeError(f"`mako init` failed; {'restored the backup' if backup else 'nothing to restore'}"
                           f" (generated files may be half-updated; re-run init to fix):\n"
                           f"{r.stdout}{r.stderr}")
    print_init(r.stdout)
    print(f"Installed:   {describe(dest, dest_tpl)}")
    print_reminders(root)


def backups(dot_mako: Path) -> list[Path]:
    """Oldest first. New style: <stamp>[-n]/ with the code (mako.py, mako_*.py) and templates/.
    Old style (single-file code only): mako.py.<stamp>[-n].bak."""
    def order(p: Path) -> tuple[str, int]:
        m = re.fullmatch(r"(?:mako\.py\.)?(\d{8}-\d{6})(?:-(\d+))?(?:\.bak)?", p.name)
        return (m.group(1), int(m.group(2) or 1))
    found = [p for p in (dot_mako / "backups").glob("*")
             if (p.is_dir() and re.fullmatch(r"\d{8}-\d{6}(-\d+)?", p.name) and (p / "mako.py").is_file())
             or (p.is_file() and re.fullmatch(r"mako\.py\.\d{8}-\d{6}(-\d+)?\.bak", p.name))]
    return sorted(found, key=order)


def rollback(root: Path, dry_run: bool) -> None:
    dot_mako = root / ".mako"
    dest = dot_mako / "mako.py"
    found = backups(dot_mako)
    if not found:
        raise UpgradeError("no backups in .mako/backups/")
    latest = found[-1]
    print(f"restore:     {latest}")
    if latest.is_dir():
        print(f"             {describe(latest / 'mako.py', latest / 'templates')}")
    else:
        print(f"             {describe(latest)}")
        print("             (old-style backup: restores the single-file mako.py only; templates/ stay as they are)")
    print(f"over:        {dest}")
    if dest.is_file():
        print(f"             {describe(dest, dot_mako / 'templates')}")
    if dry_run:
        print("Dry run; nothing changed.")
        return
    restore(latest, dot_mako)
    # Consume the backup so repeated rollbacks step further back.
    shutil.rmtree(latest) if latest.is_dir() else latest.unlink()
    print(f"Restored:    {describe(dest, dot_mako / 'templates')}")
    # Regenerate with the restored code so the generated files match it again.
    r = run_init(root, dest)
    if r.returncode != 0:
        print(f"warning: `mako init` failed after the restore; generated files may not match:\n"
              f"{r.stdout}{r.stderr}")
    else:
        print_init(r.stdout)
    print_reminders(root)


def print_init(out: str) -> None:
    lines = [s for s in out.splitlines() if s.strip()]
    print("mako init:")
    for line in lines or ["(no files changed)"]:
        print(f"  {line}")


def print_reminders(root: Path) -> None:
    print()
    print("Next:")
    print(f"  - check it works: run `mako status` and `mako list` in {root}")
    print("  - running MCP servers keep the old code until their Claude sessions restart;")
    print("    restart the orchestrator and agent panes to pick up the new version")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="upgrade", description="Install the dev Mako into a workspace's .mako/.")
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
