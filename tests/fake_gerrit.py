"""A fake Gerrit for the Gerrit backend's tests: the REST and dev-auth calls the client makes,
answered from memory by a threaded HTTP server on a free 127.0.0.1 port. No git over HTTP, so
pushes are not covered here (the real Gerrit is used for those, by hand in testing/)."""
from __future__ import annotations

import base64
import json
import re
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEV_AUTH = "DEVELOPMENT_BECOME_ANY_ACCOUNT"


class FakeGerrit:
    def __init__(self, auth_type: str = DEV_AUTH):
        self.auth_type = auth_type
        self.accounts = {1000000: {"username": "admin", "emails": set(), "tokens": {}}}
        self.sessions: dict[str, int] = {}
        self.admins = {1000000}
        self.projects: dict[str, dict] = {}
        self.access: list[dict] = []
        self.changes: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_after_accounts: int | None = None  # make account creation fail after this many
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.handle(self, "GET")

            def do_PUT(self):
                fake.handle(self, "PUT")

            def do_POST(self):
                fake.handle(self, "POST")

            def do_DELETE(self):
                fake.handle(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # state helpers for tests

    def by_name(self, username: str) -> int | None:
        return next((i for i, a in self.accounts.items() if a["username"] == username), None)

    def usernames(self) -> list[str]:
        return [a["username"] for a in self.accounts.values()]

    def add_change(self, project: str, change_id: str, topic: str, owner: str) -> int:
        number = len(self.changes) + 1
        self.changes[number] = {
            "_number": number, "project": project, "change_id": change_id, "topic": topic,
            "status": "NEW", "owner": {"username": owner},
            "revisions": {"sha1": {"_number": 1}}, "current_revision": "sha1",
            "labels": {"Code-Review": {"all": []}},
            "messages": [{"author": {"username": owner}, "_revision_number": 1, "message": "Uploaded patch set 1."}],
            "comments": {}}
        return number

    # HTTP

    def handle(self, req: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlsplit(req.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        self.calls.append((method, path))
        length = int(req.headers.get("Content-Length") or 0)
        raw = req.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw and "json" in (req.headers.get("Content-Type") or "") else raw
        except ValueError:
            body = raw
        try:
            code, data, headers = self.route(req, method, path, query, body)
        except KeyError:
            code, data, headers = 404, "Not found", {}
        text = data if isinstance(data, str) else ")]}'\n" + json.dumps(data)
        req.send_response(code)
        for key, value in headers.items():
            for v in value if isinstance(value, list) else [value]:
                req.send_header(key, v)
        payload = text.encode("utf-8") if data is not None else b""
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    def session_of(self, req) -> int | None:
        cookie = req.headers.get("Cookie") or ""
        found = re.search(r"GerritAccount=([^;]+)", cookie)
        return self.sessions.get(found.group(1)) if found else None

    def basic_user(self, req) -> int | None:
        auth = req.headers.get("Authorization") or ""
        if not auth.startswith("Basic "):
            return None
        user, _, token = base64.b64decode(auth[6:]).decode().partition(":")
        account_id = self.by_name(user)
        if account_id is not None and token in self.accounts[account_id]["tokens"].values():
            return account_id
        return None

    def sign_in(self, account_id: int):
        key = secrets.token_hex(8)
        self.sessions[key] = account_id
        return 302, None, {"Set-Cookie": f"GerritAccount={key}; Path=/", "Location": self.url + "/"}

    def route(self, req, method, path, query, body):  # noqa: PLR0911 (one return per route)
        if path == "/config/server/info":
            return 200, {"auth": {"auth_type": self.auth_type}}, {}
        if path.startswith("/login"):
            if method == "POST":
                if self.fail_after_accounts is not None and len(self.accounts) >= self.fail_after_accounts:
                    return 500, "account creation failed (test)", {}
                account_id = max(self.accounts) + 1
                self.accounts[account_id] = {"username": "", "emails": set(), "tokens": {}}
                return self.sign_in(account_id)
            account_id = int(query.get("account_id", ["0"])[0])
            return self.sign_in(account_id) if account_id in self.accounts else (200, "login form", {})
        if path == "/" and method == "GET":
            return 200, "<html>", {"Set-Cookie": "XSRF_TOKEN=xsrf; Path=/"}
        if path.startswith("/accounts/self"):
            return self.self_route(req, method, path[len("/accounts/self"):], body)
        if path.startswith("/a/"):
            user = self.basic_user(req)
            if user is None:
                return 401, "Unauthorized", {}
            return self.api_route(user, method, path[2:], query, body)
        return 404, "Not found", {}

    def self_route(self, req, method, rest, body):  # noqa: PLR0911
        account_id = self.session_of(req)
        if account_id is None:
            return 403, "not signed in", {}
        if method != "GET" and req.headers.get("X-Gerrit-Auth") != "xsrf":
            return 403, "bad xsrf", {}
        account = self.accounts[account_id]
        if rest == "" and method == "GET":
            return 200, {"_account_id": account_id, "username": account["username"]}, {}
        if rest == "/username":
            if self.by_name(body["username"]) is not None:
                return 409, "username taken", {}
            account["username"] = body["username"]
            return 200, body["username"], {}
        if rest == "/name":
            return 200, body["name"], {}
        token_id = rest[len("/tokens/"):]
        if method == "DELETE":
            return (204, None, {}) if account["tokens"].pop(token_id, None) else (404, f"Not found: {token_id}", {})
        if method == "PUT":
            if token_id in account["tokens"]:
                return 404, "Not found: /", {}
            account["tokens"][token_id] = secrets.token_urlsafe(12) + "/+"
            return 201, {"id": token_id, "token": account["tokens"][token_id]}, {}
        return 404, "Not found", {}

    def api_route(self, user, method, path, query, body):  # noqa: PLR0911
        if path == "/accounts/self":
            return 200, {"_account_id": user, "username": self.accounts[user]["username"]}, {}
        if found := re.fullmatch(r"/accounts/([^/]+)/emails/(.+)", path):
            account = self.accounts[self.by_name(found.group(1))]
            if found.group(2) in account["emails"]:
                return 409, "already registered", {}
            account["emails"].add(found.group(2))
            return 201, {"email": found.group(2)}, {}
        if found := re.fullmatch(r"/accounts/([^/]+)", path):
            account_id = self.by_name(urllib.parse.unquote(found.group(1)))
            if account_id is None:
                return 404, "Not found", {}
            return 200, {"_account_id": account_id, "username": self.accounts[account_id]["username"]}, {}
        if path == "/groups/Administrators/members":
            self.admins |= {self.by_name(name) for name in body["members"]}
            return 200, [], {}
        if path == "/groups/Administrators":
            return 200, {"id": "admins-uuid"}, {}
        if found := re.fullmatch(r"/projects/([^/]+)", path):
            name = urllib.parse.unquote(found.group(1))
            if method == "PUT":
                self.projects[name] = body
                return 201, {"name": name}, {}
            return (200, {"name": name}, {}) if name in self.projects else (404, "Not found", {})
        if re.fullmatch(r"/projects/[^/]+/access", path):
            self.access.append(body)
            return 200, {}, {}
        if path == "/changes/":
            terms = dict(t.split(":", 1) for t in query["q"][0].split())
            return 200, [c for c in self.changes.values()
                         if c["project"] == terms["project"] and c["change_id"] == terms["change"]], {}
        found = re.fullmatch(r"/changes/(\d+)/(.+)", path)
        if not found:
            return 404, "Not found", {}
        change = self.changes[int(found.group(1))]
        action = found.group(2)
        if action == "detail":
            return 200, change, {}
        if action == "comments":
            return 200, change["comments"], {}
        if action in ("abandon", "restore"):
            change["status"] = "ABANDONED" if action == "abandon" else "NEW"
            change["messages"].append({"author": {"username": self.accounts[user]["username"]},
                                       "_revision_number": 1, "message": f"{action.title()}d\n\n{body['message']}"})
            return 200, change, {}
        if action == "revisions/current/review":
            name = self.accounts[user]["username"]
            votes = [v for v in change["labels"]["Code-Review"]["all"] if v["username"] != name]
            vote = {"username": name, "value": body["labels"]["Code-Review"]}
            change["labels"]["Code-Review"]["all"] = [*votes, vote]
            change["messages"].append({"author": {"username": name}, "_revision_number": 1, "message": body["message"]})
            for file, items in body.get("comments", {}).items():
                change["comments"].setdefault(file, []).extend(
                    {**c, "patch_set": 1, "author": {"username": name}} for c in items)
            return 200, {}, {}
        return 404, "Not found", {}
