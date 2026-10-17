"""Gerrit as Mako's review backend. Standard library only; works on Python 3.8+.

Shipped in templates/gerrit/ and installed into .mako/templates/gerrit/ by `mako init`; mako.py
imports it when .mako/config.json has "review": {"backend": "gerrit", ...}. It is given a URL and a
state folder and speaks only Gerrit's HTTP API (REST, and git over HTTP for pushes). How or where the
Gerrit runs is not its business, so it needs one in "become any account" dev-auth mode.

- Accounts: the built-in `admin`, plus one per name Mako passes (`human` and the agent slots), each
  with an email under mako.example.com and a named auth token (one token id per workspace, so
  workspaces sharing a Gerrit don't revoke each other's). Tokens live in <state>/credentials.json.
  A token Gerrit no longer accepts (e.g. the Gerrit was reset) is replaced on the next call.
- One issue = one change: `push` squashes base..branch into one commit, signed as the pushing
  account, with a Change-Id derived from the issue id, topic = issue id. Every push is a new patch
  set of the same change; the agent's own history is never rewritten and no commit-msg hook is used.
- Gerrit never submits: Mako merges, then abandons the change with a note (`finish`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from pathlib import Path

ADMIN = "admin"
ADMIN_ID = 1000000  # dev auth: the built-in administrator, which Gerrit creates on the first request
DEV_AUTH = "DEVELOPMENT_BECOME_ANY_ACCOUNT"
EMAIL_DOMAIN = "mako.example.com"  # Gerrit rejects reserved TLDs (.local, .test, .invalid)
TIMEOUT = 15
APPROVE = 2  # the Code-Review score that approves a change


class GerritError(Exception):
    pass


class Unreachable(GerritError):
    """Nothing answers at the URL."""


class HttpError(GerritError):
    def __init__(self, code: int, method: str, path: str, text: str):
        super().__init__(f"{method} {path}: HTTP {code} {text.strip()[:200]}")
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def change_id(issue: str) -> str:
    """The Change-Id for an issue: stable, so every push of the issue is a patch set of one change."""
    return "I" + hashlib.sha1(issue.encode("utf-8")).hexdigest()


def review_state(detail: dict) -> str:
    """pending, changes_requested, approved, merged or abandoned, from a change's detail."""
    if detail.get("status") != "NEW":
        return str(detail.get("status", "")).lower()
    scores = [v.get("value", 0) for v in detail.get("labels", {}).get("Code-Review", {}).get("all", [])
              if v.get("value")]
    if scores and min(scores) < 0:
        return "changes_requested"
    return "approved" if scores and max(scores) >= APPROVE else "pending"


def _indent(text: str) -> str:
    """A multi-line text as the rest of a markdown list item."""
    return "\n".join(("  " + line if line.strip() else "") if i else line
                     for i, line in enumerate(text.strip().splitlines()))


def _cookie(headers, name: str) -> str:
    for line in headers.get_all("Set-Cookie") or []:
        if line.startswith(name + "="):
            return line.split(";", 1)[0].split("=", 1)[1]
    return ""


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})}, timeout=300, check=False)


def _git_out(repo: Path, *args: str, env: dict | None = None) -> str:
    r = _git(repo, *args, env=env)
    if r.returncode:
        raise GerritError((r.stderr or r.stdout).strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


class Gerrit:
    def __init__(self, url: str, state_dir, project: str, token_name: str = "mako"):
        self.url = url.rstrip("/")
        self.state_dir = Path(state_dir)
        self.project = project
        self.token_name = token_name
        self.creds_file = self.state_dir / "credentials.json"
        try:
            saved = json.loads(self.creds_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        # Tokens belong to one Gerrit: a different URL starts over.
        self.accounts = saved.get("accounts", {}) if saved.get("url") == self.url else {}

    # HTTP

    def request(self, method: str, path: str, body=None, auth: str = "", cookie: str = "",
                xsrf: str = "", follow: bool = True):
        """(data, headers). JSON bodies in and out; Gerrit's )]}' prefix is stripped."""
        headers = {}
        data = None
        if isinstance(body, bytes):
            data, headers["Content-Type"] = body, "application/x-www-form-urlencoded"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        if auth:
            headers["Authorization"] = auth
        if cookie:
            headers["Cookie"] = cookie
        if xsrf:
            headers["X-Gerrit-Auth"] = xsrf
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(*([] if follow else [_NoRedirect]))
        try:
            resp = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as err:
            if follow or err.code not in (301, 302, 303):
                text = err.read().decode("utf-8", "replace")
                raise HttpError(err.code, method, path.split("?", 1)[0], text) from None
            resp = err
        except (urllib.error.URLError, OSError) as err:
            raise Unreachable(f"Gerrit not reachable at {self.url} ({getattr(err, 'reason', err)})") from None
        text = resp.read().decode("utf-8", "replace")
        if text.startswith(")]}'"):
            text = text[4:]
        try:
            return (json.loads(text) if text.strip() else None), resp.headers
        except ValueError:
            return text, resp.headers

    def auth(self, name: str) -> str:
        account = self.accounts[name]
        return "Basic " + base64.b64encode(f"{account['username']}:{account['token']}".encode()).decode()

    def api(self, method: str, path: str, body=None, as_: str = ADMIN):
        """An authenticated REST call (/a/...) as one of our accounts. A token Gerrit refuses is
        replaced once and the call retried."""
        if as_ not in self.accounts:
            self.ensure_account(as_)
        try:
            return self.request(method, "/a" + path, body, auth=self.auth(as_))[0]
        except HttpError as err:
            if err.code != HTTPStatus.UNAUTHORIZED:
                raise
        self.ensure_account(as_, renew=True)
        return self.request(method, "/a" + path, body, auth=self.auth(as_))[0]

    def server_info(self) -> dict:
        info, _ = self.request("GET", "/config/server/info")
        auth = (info or {}).get("auth", {}).get("auth_type") if isinstance(info, dict) else None
        if auth != DEV_AUTH:
            raise GerritError(f"the Gerrit at {self.url} uses auth type {auth or 'unknown'}; Mako needs "
                              f"{DEV_AUTH} (\"become any account\", the official image's default)")
        return info

    # accounts

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.creds_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"url": self.url, "accounts": self.accounts}, indent=2), encoding="utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.creds_file)

    def session(self, account_id: int | None = None) -> tuple[str, str]:
        """Dev auth: sign in as an account (or create a new one) and return (cookie, xsrf token).
        Writes need the XSRF token as both cookie and header."""
        if account_id is None:
            _, headers = self.request("POST", "/login/%23%2F", b"action=create_account", follow=False)
        else:
            _, headers = self.request("GET", f"/login/%23%2F?account_id={account_id}", follow=False)
        account = _cookie(headers, "GerritAccount")
        if not account:
            raise GerritError(f"dev-auth sign-in {'of a new account' if account_id is None else account_id} "
                              f"failed at {self.url}")
        _, headers = self.request("GET", "/", cookie=f"GerritAccount={account}")
        xsrf = _cookie(headers, "XSRF_TOKEN")
        return f"GerritAccount={account}; XSRF_TOKEN={xsrf}", xsrf

    def new_token(self, cookie: str, xsrf: str) -> str:
        path = f"/accounts/self/tokens/{urllib.parse.quote(self.token_name, safe='')}"
        try:
            self.request("DELETE", path, cookie=cookie, xsrf=xsrf)
        except HttpError as err:
            if err.code != HTTPStatus.NOT_FOUND:
                raise
        try:
            token, _ = self.request("PUT", path, {}, cookie=cookie, xsrf=xsrf)
            return token["token"]
        except HttpError as err:
            if err.code not in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED):
                raise
        # Before Gerrit 3.13 there are no named tokens, only one HTTP password per account.
        password, _ = self.request("PUT", "/accounts/self/password.http", {"generate": True}, cookie=cookie, xsrf=xsrf)
        return password

    def find_account(self, name: str) -> int | None:
        try:
            return self.api("GET", f"/accounts/{urllib.parse.quote(name, safe='')}")["_account_id"]
        except HttpError as err:
            if err.code == HTTPStatus.NOT_FOUND:
                return None
            raise

    def ensure_account(self, name: str, renew: bool = False) -> None:
        """Make sure we hold a token for `name`: sign in as the existing account (looked up by
        username) or create it, then make a token. Saved at once, so a setup that fails half way
        resumes without making accounts twice."""
        if name in self.accounts and not renew:
            return
        account_id = ADMIN_ID if name == ADMIN else self.find_account(name)
        if account_id is None:
            cookie, xsrf = self.session()
            self.request("PUT", "/accounts/self/username", {"username": name}, cookie=cookie, xsrf=xsrf)
            self.request("PUT", "/accounts/self/name", {"name": name}, cookie=cookie, xsrf=xsrf)
        else:
            cookie, xsrf = self.session(account_id)
        token = self.new_token(cookie, xsrf)
        me, _ = self.request("GET", "/accounts/self", cookie=cookie)
        self.accounts[name] = {"id": me["_account_id"], "username": me.get("username", name), "token": token}
        self.save()
        if name != ADMIN:
            self.register_email(name)

    def register_email(self, name: str) -> None:
        """Commits are made as <name>@mako.example.com, and Gerrit checks the committer's email."""
        try:
            self.api("PUT", f"/accounts/{name}/emails/{name}@{EMAIL_DOMAIN}", {"no_confirmation": True})
        except HttpError as err:
            if err.code != HTTPStatus.CONFLICT:  # already registered
                raise

    # setup

    def setup(self, people: list[str], repo: Path, base: str) -> list[str]:
        """Accounts for `people` (`human` becomes an administrator), the project with push rights
        for administrators, and the base branch. Idempotent. Returns what it did."""
        self.server_info()
        done = []
        for name in [ADMIN, *people]:
            known = name in self.accounts
            self.ensure_account(name)
            if known:
                self.api("GET", "/accounts/self", as_=name)  # refreshes a token the Gerrit forgot
            if not known:
                done.append(f"account {name}")
        if "human" in people:
            self.api("POST", "/groups/Administrators/members", {"members": ["human"]})  # may vote +2
            # An administrator's first sign-in lands on an intro page instead of where the link
            # points; use it up here, so the sign-in links Mako hands out open the change.
            self.session(self.accounts["human"]["id"])
        project = urllib.parse.quote(self.project, safe="")
        try:
            self.api("GET", f"/projects/{project}")
        except HttpError as err:
            if err.code != HTTPStatus.NOT_FOUND:
                raise
            self.api("PUT", f"/projects/{project}", {"create_empty_commit": True,
                                                     "description": "Code review for a Mako workspace"})
            done.append(f"project {self.project}")
        admins = self.api("GET", "/groups/Administrators")
        rule = {"rules": {admins["id"]: {"action": "ALLOW", "force": True}}}
        # The base branch shares no history with the project's empty first commit: let
        # administrators push it (forced) and push other people's commits.
        self.api("POST", f"/projects/{project}/access", {"add": {"refs/heads/*": {"permissions": {
            "push": rule, "create": rule, "forgeAuthor": rule, "forgeCommitter": rule, "pushMerge": rule}}}})
        self.sync_base(repo, base)
        return done

    def ready(self, people: list[str]) -> bool:
        """Cheap check that setup has been done against this Gerrit."""
        if any(name not in self.accounts for name in [ADMIN, *people]):
            return False
        try:
            self.api("GET", f"/projects/{urllib.parse.quote(self.project, safe='')}")
        except HttpError as err:
            if err.code == HTTPStatus.NOT_FOUND:
                return False
            raise
        return True

    # git over HTTP

    @property
    def remote(self) -> str:
        return f"{self.url}/a/{urllib.parse.quote(self.project, safe='')}"

    def git_push(self, repo: Path, refspec: str, as_: str, force: bool = False) -> subprocess.CompletedProcess:
        """Push as one of our accounts. The token goes in through the environment, so it is not on
        the command line; git's own credential helpers and prompts are kept out of it."""
        for attempt in (1, 2):
            env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
                   "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                   "GIT_CONFIG_KEY_1": "http.extraHeader", "GIT_CONFIG_VALUE_1": f"Authorization: {self.auth(as_)}"}
            r = _git(repo, "push", *(["--force"] if force else []), self.remote, refspec, env=env)
            if attempt == 1 and r.returncode and ("401" in r.stderr or "Authentication failed" in r.stderr):
                self.ensure_account(as_, renew=True)
                continue
            if r.returncode and ("Could not resolve host" in r.stderr or "Failed to connect" in r.stderr
                                 or "Connection refused" in r.stderr):
                raise Unreachable(f"Gerrit not reachable at {self.url}")
            return r
        return r

    def sync_base(self, repo: Path, base: str) -> None:
        """Gerrit's copy of the base branch follows the workspace's, so a change's parent is known."""
        r = self.git_push(repo, f"refs/heads/{base}:refs/heads/{base}", ADMIN, force=True)
        if r.returncode:
            raise GerritError(f"could not update {base} in Gerrit: {r.stderr.strip()}")

    # changes

    def squash(self, repo: Path, issue: str, account: str, branch: str, base: str, title: str) -> str:
        """One commit with base..branch's changes on top of their merge base, signed as `account`,
        dated like the branch tip so an unchanged branch gives the same commit (no empty patch set)."""
        merge_base = _git_out(repo, "merge-base", base, branch)
        commits = _git_out(repo, "log", "--no-merges", "--format=- %h %s", f"{merge_base}..{branch}")
        if _git_out(repo, "rev-parse", f"{branch}^{{tree}}") == _git_out(repo, "rev-parse", f"{merge_base}^{{tree}}"):
            raise GerritError(f"nothing to review: {branch} makes no changes to {base}")
        stamp = _git_out(repo, "log", "-1", "--format=%cI", branch)
        email = f"{account}@{EMAIL_DOMAIN}"
        env = {"GIT_AUTHOR_NAME": account, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
               "GIT_COMMITTER_NAME": account, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp}
        message = f"{issue}: {title}\n\n{commits}\n\nChange-Id: {change_id(issue)}\n".replace("\n\n\n\n", "\n\n")
        return _git_out(repo, "commit-tree", f"{branch}^{{tree}}", "-p", merge_base, "-m", message, env=env)

    def find_change(self, issue: str) -> dict | None:
        query = urllib.parse.quote(f"project:{self.project} change:{change_id(issue)}", safe=":")
        found = self.api("GET", f"/changes/?q={query}&o=CURRENT_REVISION&o=DETAILED_ACCOUNTS")
        return found[0] if found else None

    def push(self, repo: Path, issue: str, account: str, branch: str, base: str, title: str) -> dict:
        """Upload the issue as a new patch set of its change (made on the first push). Returns
        {change, patch_set, new}; new is False when Gerrit already had this exact commit."""
        self.sync_base(repo, base)
        commit = self.squash(repo, issue, account, branch, base, title)
        existing = self.find_change(issue)
        if existing and existing["status"] == "ABANDONED":  # released or reopened: same change again
            self.api("POST", f"/changes/{existing['_number']}/restore",
                     {"message": f"Restored by Mako: {issue} is in review again."})
        r = self.git_push(repo, f"{commit}:refs/for/{base}%topic={issue}", account)
        if r.returncode and "no new changes" not in r.stderr:
            raise GerritError(f"push of {issue} to Gerrit failed: {r.stderr.strip()}")
        change = self.find_change(issue)
        if not change:
            raise GerritError(f"pushed {issue}, but Gerrit has no change for it")
        revision = change.get("revisions", {}).get(change.get("current_revision"), {})
        return {"change": change["_number"], "patch_set": revision.get("_number"), "new": not r.returncode}

    def change_url(self, number: int) -> str:
        return f"{self.url}/c/{urllib.parse.quote(self.project, safe='')}/+/{number}"

    def sign_in_url(self, number: int, name: str = "human") -> str:
        """Dev auth: opening this signs in as `name` and lands on the change, no login screen."""
        path = urllib.parse.quote(f"/c/{self.project}/+/{number}", safe="")
        return f"{self.url}/login/{path}?account_id={self.accounts[name]['id']}"

    def detail(self, issue: str) -> tuple[dict, dict]:
        change = self.find_change(issue)
        if not change:
            raise GerritError(f"Gerrit has no change for {issue}; call request_review first")
        number = change["_number"]
        full = self.api("GET", f"/changes/{number}/detail?o=DETAILED_LABELS&o=MESSAGES&o=ALL_REVISIONS"
                               "&o=DETAILED_ACCOUNTS")
        return full, self.api("GET", f"/changes/{number}/comments") or {}

    def status(self, issue: str) -> dict:
        full, comments = self.detail(issue)
        votes = {label: {v.get("username", str(v.get("_account_id"))): v["value"]
                         for v in info.get("all", []) if v.get("value")}
                 for label, info in full.get("labels", {}).items()}
        inline = [{"path": path, "line": c.get("line"), "patch_set": c.get("patch_set"),
                   "author": c.get("author", {}).get("username"), "message": c.get("message", ""),
                   "unresolved": c.get("unresolved", False)}
                  for path, items in sorted(comments.items()) for c in items]
        messages = [{"author": m.get("author", {}).get("username", "gerrit"), "patch_set": m.get("_revision_number"),
                     "message": m.get("message", "")} for m in full.get("messages", [])]
        patch_sets = [rev["_number"] for rev in full.get("revisions", {}).values()]
        return {"issue": issue, "change": full["_number"], "url": self.change_url(full["_number"]),
                "state": review_state(full), "owner": full.get("owner", {}).get("username"),
                "patch_set": max(patch_sets, default=None), "votes": votes, "comments": inline,
                "messages": messages}

    def transcript(self, issue: str) -> str:
        """Markdown history of the review: every message and inline comment, with author and patch set."""
        s = self.status(issue)
        lines = [f"# Review of {issue}", "",
                 f"Gerrit change [{s['change']}]({s['url']}), owner **{s['owner']}**, {s['state']}, "
                 f"{s['patch_set']} patch set{'s' if s['patch_set'] != 1 else ''}.", "", "## Messages", ""]
        for msg in s["messages"]:
            text = _indent(msg["message"])
            lines.append(f"- **{msg['author']}** (patch set {msg['patch_set'] or '-'}): {text}")
        if s["comments"]:
            lines += ["", "## Inline comments", ""]
            for c in s["comments"]:
                where = f"{c['path']}:{c['line']}" if c["line"] else c["path"]
                text = _indent(c["message"])
                lines.append(f"- patch set {c['patch_set']}, `{where}`, **{c['author']}**: {text}")
        return "\n".join(lines) + "\n"

    def finish(self, issue: str, message: str) -> int | None:
        """Abandon the issue's change with a note (Mako did any merge); patch sets and comments stay.
        Returns the change number, or None when there is no open change."""
        change = self.find_change(issue)
        if not change or change["status"] != "NEW":
            return None
        self.api("POST", f"/changes/{change['_number']}/abandon", {"message": message})
        return change["_number"]

    def vote(self, issue: str, name: str, score: int, message: str = "", comments: dict | None = None) -> None:
        """Review as `name` (for testing and scripts): a Code-Review score, a message and inline
        comments as {path: [{"line": n, "message": text}]}."""
        change = self.find_change(issue)
        if not change:
            raise GerritError(f"Gerrit has no change for {issue}")
        body = {"message": message, "labels": {"Code-Review": score}, "comments": comments or {}}
        self.api("POST", f"/changes/{change['_number']}/revisions/current/review", body, as_=name)
