"""Same-origin admin API, password sessions, and bundled static UI."""

import hashlib
import json
import re
import secrets
import threading
import time
from collections import deque
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

from . import upstream

STATIC = Path(__file__).with_name("static")
ASSETS = {"/admin": ("admin.html", "text/html; charset=utf-8"),
          "/admin/admin.css": ("admin.css", "text/css; charset=utf-8"),
          "/admin/docs.js": ("docs.js", "text/javascript; charset=utf-8"),
          "/admin/admin.js": ("admin.js", "text/javascript; charset=utf-8")}


def security_headers(handler):
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class Admin:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.lock = threading.Lock()
        self.sessions = {}
        self.attempts = {}

    def _rate_allowed(self, address):
        now = time.monotonic()
        with self.lock:
            self.attempts = {k: deque(t for t in v if t > now - 60) for k, v in self.attempts.items() if v and v[-1] > now - 60}
            if len(self.attempts) > 256:
                return False
            for key, limit in (("global", 30), (address, 8)):
                if len(self.attempts.get(key, ())) >= limit:
                    return False
            for key in ("global", address):
                self.attempts.setdefault(key, deque()).append(now)
            return True

    @staticmethod
    def _cookie_token(handler):
        try:
            cookies = SimpleCookie(handler.headers.get("Cookie", ""))
            return cookies["n2a_session"].value if "n2a_session" in cookies else ""
        except Exception:
            return ""

    def _session(self, handler):
        token = self._cookie_token(handler)
        hashed = hashlib.sha256(token.encode()).hexdigest()
        version = self.store.setting("auth_version")
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > time.time() and v["version"] == version}
            return self.sessions.get(hashed)

    def _new_session(self, version):
        token = secrets.token_urlsafe(32)
        session = {"csrf": secrets.token_urlsafe(24), "version": version, "expires": time.time() + 28800}
        with self.lock:
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = session
        return token, session["csrf"]

    @staticmethod
    def _same_origin(handler):
        if handler.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return False
        origin = handler.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            return parsed.scheme in ("http", "https") and parsed.netloc.lower() == handler.headers.get("Host", "").lower()
        return True

    @staticmethod
    def _send(handler, status, payload, cookie=None):
        blob = json.dumps(payload, ensure_ascii=False).encode()
        handler.close_connection = True
        handler.send_response(status)
        security_headers(handler)
        if cookie is not None:
            # Nginx must overwrite X-Forwarded-Proto; it must not append a client value.
            secure = "; Secure" if handler.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
            handler.send_header("Set-Cookie", "n2a_session=" + cookie + "; Path=/admin; HttpOnly; SameSite=Strict; Max-Age="
                                + ("28800" if cookie else "0") + secure)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(blob)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(blob)

    def handle(self, handler, path):
        if handler.command == "GET" and path in ASSETS:
            name, content_type = ASSETS[path]
            blob = (STATIC / name).read_bytes()
            handler.close_connection = True
            handler.send_response(200)
            security_headers(handler)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(blob)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(blob)
            return
        send = lambda status, payload, cookie=None: self._send(handler, status, payload, cookie)
        if not path.startswith("/admin/api/"):
            send(404, {"error": "页面不存在"})
            return
        session = self._session(handler)
        if handler.command == "GET" and path == "/admin/api/status":
            send(200, {"setup_required": not bool(self.store.setting("password")), "authenticated": bool(session),
                       "csrf": session["csrf"] if session else None})
            return
        if handler.command == "GET" and path == "/admin/api/state":
            if not session:
                send(401, {"error": "请先登录"})
            else:
                send(200, self.store.dashboard())
            return
        if handler.command != "POST":
            send(404, {"error": "接口不存在"})
            return
        if not self._same_origin(handler):
            send(403, {"error": "不允许跨站操作"})
            return
        public = path in ("/admin/api/setup", "/admin/api/login")
        if not public:
            if not session:
                send(401, {"error": "登录已过期，请重新登录"})
                return
            if not secrets.compare_digest(handler.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()):
                send(403, {"error": "安全校验失败，请刷新页面"})
                return
        if (public or path == "/admin/api/password") and not self._rate_allowed(handler.client_address[0]):
            send(429, {"error": "操作过于频繁，请一分钟后重试"})
            return
        # Admin bodies only contain names/passwords/keys, never large model inputs.
        body = handler._read_json(max_bytes=16384)
        if body is None:
            return
        try:
            if path == "/admin/api/setup":
                code = body.get("code", "")
                if not isinstance(code, str) or len(code) > 256:
                    raise ValueError("初始化码无效")
                version = self.store.setup(code, body.get("password"))
                token, csrf = self._new_session(version)
                send(200, {"csrf": csrf}, token)
            elif path == "/admin/api/login":
                version = self.store.authenticate(body.get("password"))
                if not version:
                    send(401, {"error": "密码不正确"})
                    return
                token, csrf = self._new_session(version)
                send(200, {"csrf": csrf}, token)
            elif path == "/admin/api/logout":
                with self.lock:
                    self.sessions.pop(hashlib.sha256(self._cookie_token(handler).encode()).hexdigest(), None)
                send(200, {"ok": True}, "")
            elif path == "/admin/api/client-token":
                send(200, {"token": self.store.setting("client_token")})
            elif path == "/admin/api/key-import-token":
                send(200, {"token": self.store.setting("key_import_token")})
            elif path == "/admin/api/rotate-token":
                if body.get("confirm") is not True:
                    raise ValueError("请确认更换访问令牌")
                send(200, {"token": self.store.rotate_token()})
            elif path == "/admin/api/password":
                version = self.store.change_password(body.get("old_password"), body.get("password"))
                token, csrf = self._new_session(version)
                send(200, {"csrf": csrf}, token)
            elif path == "/admin/api/accounts":
                send(200, {"id": self.store.save_account(body)})
            else:
                match = re.fullmatch(r"/admin/api/accounts/([1-9][0-9]{0,9})/(edit|delete|test)", path)
                if not match:
                    send(404, {"error": "接口不存在"})
                    return
                account_id, action = int(match[1]), match[2]
                if action == "edit":
                    self.store.save_account(body, account_id)
                    send(200, {"ok": True})
                elif action == "delete":
                    self.store.delete_account(account_id)
                    send(200, {"ok": True})
                else:
                    account = self.store.account(account_id)
                    try:
                        upstream._request(self.cfg.base_url + "/models", account["api_key"], timeout=15, retries=0)
                        status = 200
                    except upstream.UpstreamError as exc:
                        status = exc.status
                    self.store.record(account, "models", status, 0, probe=True)
                    # Do not return an arbitrary upstream payload that may echo a key.
                    messages = {200: "连接成功，可读取模型列表", 401: "Key 无效，请检查后重新填写",
                                403: "账户没有访问权限，请确认已开通 AI Gateway", 429: "上游限流，稍后重试",
                                504: "连接超时，请检查服务器网络"}
                    send(200, {"ok": status == 200, "status": status, "message": messages.get(status, "上游返回错误（HTTP %s）" % status)})
        except ValueError as exc:
            send(400, {"error": str(exc)})
