"""Control-plane and account-pool integration tests. All upstream traffic is local."""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from nutrient2api import server
from nutrient2api.storage import Store
from test_regression import MockGateway, completion_chunk, parse_sse


class AccountGateway(MockGateway):
    def key(self):
        key = self.headers.get("Authorization", "")[7:]
        self.server.calls.append((self.command, key))
        return key

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        key = self.key()
        status = self.server.statuses.get(key, 200)
        # Deliberately malicious error payload: must not be echoed by the probe.
        self.reply(status, {"data": []} if status == 200 else {"error": {"message": key}})

    def do_POST(self):
        key = self.key()
        status = self.server.statuses.get(key, 200)
        if status != 200:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(status, {"error": {"message": "rejected"}})
        else:
            super().do_POST()


class AdminHTTP(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"NUTRIENT2API_QUIET": "1", "NUTRIENT2API_DATA_DIR": self.directory.name}, clear=True)
        self.env.start()
        self.cfg = server.Config()
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), AccountGateway)
        self.gateway.mode = "ok"
        self.gateway.requests, self.gateway.calls, self.gateway.statuses = [], [], {}
        self.gateway.events = [completion_chunk("hello"), completion_chunk("", finish="stop")]
        self.cfg.base_url = "http://127.0.0.1:%s/v1" % self.gateway.server_port
        self.httpd = server.ProxyHTTPServer(("127.0.0.1", 0), self.cfg)
        self.store = self.httpd.store
        self.threads = []
        for instance in (self.gateway, self.httpd):
            thread = threading.Thread(target=lambda s=instance: s.serve_forever(poll_interval=0.01))
            thread.start()
            self.threads.append(thread)
        self.cookie, self.csrf = "", ""
        self.password = "offline-admin-password"
        self.payload = {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "do not store this prompt"}]}

    def tearDown(self):
        for instance in (self.httpd, self.gateway):
            instance.shutdown()
            instance.server_close()
        for thread in self.threads:
            thread.join(3)
        self.env.stop()
        self.directory.cleanup()

    def request(self, path, body=None, headers=None, auth=True, raw=False, method=None):
        request_headers = {"Content-Type": "application/json", "Connection": "close"}
        if auth:
            request_headers.update({"Cookie": self.cookie, "X-CSRF-Token": self.csrf,
                                    "Authorization": "Bearer " + self.cfg.client_token})
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=6)
        try:
            conn.request(method or ("POST" if body is not None else "GET"), path,
                         json.dumps(body).encode() if body is not None else None, request_headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, data if raw else json.loads(data), dict(response.getheaders())
        finally:
            conn.close()

    def setup_admin(self):
        status, result, headers = self.request("/admin/api/setup", {"code": self.store.setting("setup_code"), "password": self.password})
        self.assertEqual(status, 200, result)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = result["csrf"]

    def add(self, name="主账户", key="offline-account-key-one"):
        return self.store.save_account({"name": name, "api_key": key})

    def test_start_empty_public_health_protected_api_and_static_assets(self):
        self.assertEqual(self.request("/health", auth=False)[0], 200)
        self.assertEqual(self.request("/v1/models", auth=False)[0], 401)
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 503)
        self.assertEqual(self.request("/admin/api/state", auth=False)[0], 401)
        status, data, _ = self.request("/admin/api/status", auth=False)
        self.assertTrue(data["setup_required"])
        self.assertIsNone(data["csrf"])
        for path in ("/admin/", "/admin/admin.css", "/admin/docs.js", "/admin/admin.js"):
            status, raw, headers = self.request(path, auth=False, raw=True)
            self.assertEqual(status, 200)
            self.assertGreater(len(raw), 100)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/admin/../storage.py", raw=True)[0], 404)

    def test_docs_import_token_requires_admin_session_csrf_and_same_origin(self):
        path = "/admin/api/key-import-token"
        self.assertEqual(self.request(path, {}, auth=False)[0], 401)
        self.setup_admin()
        self.assertEqual(self.request(path, {}, auth=False,
                                      headers={"Authorization": "Bearer " + self.cfg.client_token})[0], 401)
        self.assertEqual(self.request(path, {}, {"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(self.request(path, {}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request(path)[0], 404)
        status, result, headers = self.request(path, {})
        self.assertEqual(status, 200)
        self.assertEqual(result["token"], self.store.setting("key_import_token"))
        self.assertNotEqual(result["token"], self.cfg.client_token)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn(result["token"], json.dumps(self.request("/admin/api/state")[1]))
        rotated = self.store.rotate_key_import_token()
        self.assertEqual(self.request(path, {})[1]["token"], rotated)
        self.request("/admin/api/logout", {})
        self.assertEqual(self.request(path, {})[0], 401)

    def test_setup_requires_code_and_is_one_time(self):
        code = self.store.setting("setup_code")
        self.assertEqual(self.request("/admin/api/setup", {"code": "wrong", "password": self.password})[0], 400)
        self.assertEqual(self.request("/admin/api/setup", {"code": code, "password": "short"})[0], 400)
        self.setup_admin()
        self.assertEqual(self.store.setting("setup_code"), "")
        self.assertNotEqual(self.store.setting("password"), self.password)
        self.assertEqual(self.request("/admin/api/setup", {"code": code, "password": self.password})[0], 400)

    def test_setup_race_has_only_one_winner(self):
        body = {"code": self.store.setting("setup_code"), "password": self.password}
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: self.request("/admin/api/setup", body)[0], range(2)))
        self.assertEqual(sorted(statuses), [200, 400])

    def test_login_cookie_csrf_origin_logout_and_expiry(self):
        self.setup_admin()
        status, result, headers = self.request("/admin/api/login", {"password": self.password}, {"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/admin"):
            self.assertIn(flag, headers["Set-Cookie"])
        self.assertEqual(self.request("/admin/api/client-token", {}, {"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(self.request("/admin/api/client-token", {}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/admin/api/login", {"password": self.password}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/admin/api/state")[0], 200)
        self.assertEqual(self.request("/admin/api/logout", {})[0], 200)
        self.assertEqual(self.request("/admin/api/state")[0], 401)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/admin/api/state")[0], 200)
        for session in self.httpd.admin.sessions.values():
            session["expires"] = time.time() - 1
        self.assertEqual(self.request("/admin/api/state")[0], 401)

    def test_login_rate_limit(self):
        self.setup_admin()
        statuses = [self.request("/admin/api/login", {"password": "wrong"})[0] for _ in range(8)]
        self.assertEqual(statuses[-1], 429)
        self.assertIn(401, statuses)

    def test_account_crud_masks_keys_and_preserves_blank_edit(self):
        self.setup_admin()
        key = "offline-private-nutrient-key-1001"
        status, result, _ = self.request("/admin/api/accounts", {"name": "主账户 <script>", "api_key": key})
        self.assertEqual(status, 200)
        account_id = result["id"]
        _, data, headers = self.request("/admin/api/state")
        self.assertNotIn(key, json.dumps(data))
        self.assertEqual(data["accounts"][0]["key_mask"], "•••• 1001")
        self.assertNotIn("api_key", data["accounts"][0])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/admin/api/accounts", {"name": "重复", "api_key": key})[0], 400)
        self.assertEqual(self.request(f"/admin/api/accounts/{account_id}/edit", {"name": "修改后", "api_key": "", "enabled": False})[0], 200)
        self.assertEqual(self.store.account(account_id)["api_key"], key)
        self.assertFalse(self.store.account(account_id)["enabled"])
        self.assertEqual(self.request(f"/admin/api/accounts/{account_id}/delete", {})[0], 200)
        self.assertEqual(self.store.dashboard()["accounts"], [])

    def test_bad_account_inputs_and_admin_body_limit(self):
        self.setup_admin()
        for body in ({"name": "", "api_key": "key"}, {"name": "a", "api_key": "has spaces"},
                     {"name": "a", "api_key": "\r\nsecret"}, {"name": "a", "api_key": None},
                     {"name": "a", "api_key": "key", "enabled": "false"}):
            self.assertEqual(self.request("/admin/api/accounts", body)[0], 400)
        self.assertEqual(self.request("/admin/api/accounts", {"name": "a" * 17000})[0], 413)

    def test_probe_get_only_no_paid_calls_no_secret_echo_or_request_count(self):
        self.setup_admin()
        key = "offline-probe-key-2002"
        account_id = self.add(key=key)
        self.assertEqual(self.request(f"/admin/api/accounts/{account_id}/test", {})[1]["ok"], True)
        self.gateway.statuses[key] = 403
        status, result, _ = self.request(f"/admin/api/accounts/{account_id}/test", {})
        self.assertEqual(result["status"], 403)
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual(self.gateway.calls, [("GET", key), ("GET", key)])
        self.assertEqual(self.store.dashboard()["total_requests"], 0)

    def test_token_rotation_changes_actual_api_auth(self):
        self.setup_admin()
        old = self.cfg.client_token
        self.assertEqual(self.request("/admin/api/client-token", {})[1]["token"], old)
        self.assertEqual(self.request("/admin/api/rotate-token", {})[0], 400)
        new = self.request("/admin/api/rotate-token", {"confirm": True})[1]["token"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.request("/v1/models")[0], 401)
        self.assertEqual(self.request("/v1/models", headers={"Authorization": "Bearer " + new})[0], 200)

    def test_password_change_invalidates_other_sessions(self):
        self.setup_admin()
        old_cookie = self.cookie
        self.assertEqual(self.request("/admin/api/password", {"old_password": "wrong", "password": self.password + "new"})[0], 400)
        status, result, headers = self.request("/admin/api/password", {"old_password": self.password, "password": self.password + "new"})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/admin/api/state", headers={"Cookie": old_cookie})[0], 401)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/admin/api/state")[0], 200)
        self.assertEqual(self.request("/admin/api/login", {"password": self.password})[0], 401)

    def test_persistence_env_import_only_once_and_external_reset(self):
        self.setup_admin()
        self.add()
        original_token = self.store.setting("client_token")
        reopened = Store(self.directory.name, "unexpected-env-key", "unexpected-env-token")
        self.assertEqual(reopened.setting("client_token"), original_token)
        self.assertEqual(len(reopened.dashboard()["accounts"]), 1)
        self.assertEqual(reopened.authenticate(self.password), self.store.setting("auth_version"))
        code = reopened.reset_admin()
        self.assertEqual(self.request("/admin/api/state")[0], 401)
        self.assertEqual(self.request("/admin/api/setup", {"code": code, "password": self.password + "new"})[0], 200)
        self.assertEqual(self.store.setting("client_token"), original_token)
        self.assertEqual(len(self.store.dashboard()["accounts"]), 1)

    def test_two_keys_round_robin_disabled_excluded_and_stats_persist(self):
        first = self.add("账户一", "offline-one")
        self.add("账户二", "offline-two")
        for _ in range(4):
            self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 200)
        self.assertEqual([key for _, key in self.gateway.calls], ["offline-one", "offline-two"] * 2)
        self.store.save_account({"name": "账户一", "enabled": False}, first)
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 200)
        self.assertEqual(self.gateway.calls[-1][1], "offline-two")
        data = Store(self.directory.name).dashboard()
        self.assertEqual(data["total_requests"], 5)
        self.assertEqual(data["total_successes"], 5)
        self.assertNotIn("do not store this prompt", json.dumps(data))
        self.assertEqual(len(data["requests"]), 5)

    def test_rejection_failover_for_chat_stream_and_embeddings(self):
        for code, streaming, embeddings in ((401, False, False), (429, True, False), (403, False, True)):
            with self.subTest(code=code):
                for item in self.store.dashboard()["accounts"]:
                    self.store.delete_account(item["id"])
                self.gateway.calls.clear()
                bad = self.add("异常账户", "offline-bad")
                self.add("可用账户", "offline-good")
                self.gateway.statuses["offline-bad"] = code
                body = {"model": "text-embedding-3-small", "input": "hello"} if embeddings else dict(self.payload, stream=streaming)
                status, data, _ = self.request("/v1/embeddings" if embeddings else "/v1/chat/completions", body, raw=streaming)
                self.assertEqual(status, 200)
                self.assertEqual(self.gateway.calls, [("POST", "offline-bad"), ("POST", "offline-good")])
                self.assertGreater(self.store.account(bad)["cooldown_until"], time.time())
                if streaming:
                    self.assertEqual(parse_sse(data)[-1], "[DONE]")

    def test_server_error_does_not_replay_on_another_account(self):
        self.add("一", "offline-first")
        self.add("二", "offline-second")
        self.gateway.statuses["offline-first"] = 500
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 500)
        self.assertEqual(self.gateway.calls, [("POST", "offline-first")])

    def test_midstream_failure_does_not_switch_and_is_recorded(self):
        self.add("一", "offline-first")
        self.add("二", "offline-second")
        self.gateway.mode = "truncated"
        status, raw, _ = self.request("/v1/chat/completions", dict(self.payload, stream=True), raw=True)
        self.assertEqual(status, 200)
        self.assertIn("error", parse_sse(raw)[-2])
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.store.dashboard()["requests"][0]["status"], 502)
        self.assertEqual(self.store.dashboard()["total_successes"], 0)

    def test_cooldown_skips_all_rejected_keys_then_recovers(self):
        account_id = self.add(key="offline-bad")
        self.gateway.statuses["offline-bad"] = 429
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 429)
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 503)
        self.assertEqual(len(self.gateway.calls), 1)
        self.store.save_account({"name": "新 Key", "api_key": "offline-fixed"}, account_id)
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 200)

    def test_error_in_successful_sse_response_is_not_counted_as_success(self):
        self.add("一", "offline-first")
        self.add("二", "offline-second")
        self.gateway.events = [completion_chunk("hello"), {"error": {"message": "upstream failed"}}]
        status, raw, _ = self.request("/v1/chat/completions", dict(self.payload, stream=True), raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(parse_sse(raw)[-2]["error"]["message"], "upstream failed")
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.store.dashboard()["total_successes"], 0)
        self.assertEqual(self.store.dashboard()["requests"][0]["status"], 502)

    def test_first_sse_error_returns_json_before_headers_without_replay(self):
        self.add("一", "offline-first")
        self.add("二", "offline-second")
        self.gateway.events = [{"error": {"message": "failed before output"}}]
        status, data, headers = self.request("/v1/chat/completions", dict(self.payload, stream=True))
        self.assertEqual(status, 502)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(data["error"]["message"], "failed before output")
        self.assertEqual(len(self.gateway.calls), 1)

    def test_bounded_logs_and_key_change_does_not_inherit_old_health(self):
        account_id = self.add()
        old = self.store.account(account_id)
        self.store.save_account({"name": "新 Key", "api_key": "offline-fixed"}, account_id)
        self.store.record(old, "gpt-6-luna", 401, 30)
        self.assertEqual(self.store.account(account_id)["last_status"], 0)
        for _ in range(205):
            self.store.record(old, "gpt-6-luna", 200, 10)
        data = self.store.dashboard()
        self.assertEqual(len(data["requests"]), 200)
        self.assertEqual(data["total_requests"], 206)


if __name__ == "__main__":
    unittest.main()
