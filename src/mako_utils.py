"""Generic helpers: errors, time, text, files and processes."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path


VERSION = "0.1.0"


class MakoError(Exception):
    pass


def now() -> str:
    return os.environ.get("MAKO_NOW") or datetime.now().strftime("%Y-%m-%d %H:%M")  # MAKO_NOW: tests


def minutes_between(start: str, end: str) -> float | None:
    """Minutes from one card timestamp to another, or None if either doesn't parse."""
    try:
        fmt = "%Y-%m-%d %H:%M"
        return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "issue"


def write_atomic(path: Path, text: str) -> None:
    # Dot-prefixed temp file so Obsidian never indexes it. Retry the replace
    # because Obsidian can briefly hold the target open on Windows.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # write_text(newline=) is 3.10+
        f.write(text)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def start_shell(cmd: str, cwd: Path, out, env: dict | None, flags: int = 0) -> subprocess.Popen:
    """Start a shell command in a process group of its own. Through the shell so things like
    npx (a .cmd on Windows) resolve; stdin is DEVNULL so it can never read the MCP server's
    stdin, and output goes to `out`. On Windows it gets a hidden console of its own (console
    children stay hidden too), plus any extra creation flags."""
    kw = dict(cwd=str(cwd), shell=True, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env)
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=True, **kw)
    return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                            | flags, **kw)


def spawn_detached(cmd: str, cwd: Path, out, env: dict | None = None) -> None:
    """Start a shell command and don't wait for it. On Windows it leaves the caller's job
    if allowed, so it outlives the MCP server."""
    if os.name != "nt":
        start_shell(cmd, cwd, out, env)
        return
    try:
        start_shell(cmd, cwd, out, env, subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:  # the job doesn't allow breakaway
        start_shell(cmd, cwd, out, env)


def run_and_wait(cmd: str, cwd: Path, out, env: dict | None, timeout: float | None) -> int | None:
    """Run a shell command to the end (see start_shell). Returns its exit code, or None if it
    ran past timeout (seconds), when its whole process tree is killed."""
    proc = start_shell(cmd, cwd, out, env)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            with suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        return None
