"""Headless key intake: real loopback HTTP, isolated storage, no external calls."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_admin
from nutrient2api.storage import Store

HOST_SYSTEM_ROOT = os.environ.get("SystemRoot")


class KeyReceiveHTTP(unittest.TestCase):
    setUp = test_admin.AdminHTTP.setUp
    tearDown = test_admin.AdminHTTP.tearDown
    request = test_admin.AdminHTTP.request
    setup_admin = test_admin.AdminHTTP.setup_admin

    def submit(self, body=None, token=None, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = "Bearer " + (self.store.setting("key_import_token") if token is None else token)
        return self.request("/api/keys", {"api_key": "offline-received-key-1001"} if body is None else body,
                            headers=headers, **kwargs)

    def cli(self, command):
        env = os.environ.copy()
        # Python's Windows randomness provider needs SystemRoot, which the
        # shared server fixture intentionally clears with other host settings.
        if HOST_SYSTEM_ROOT:
            env["SystemRoot"] = HOST_SYSTEM_ROOT
        result = subprocess.run([sys.executable, "-B", "-m", "nutrient2api", command],
                                cwd=str(Path(__file__).resolve().parents[1]), env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout.strip()

    def test_requires_import_token_and_does_not_accept_client_token_or_cookie(self):
        self.assertEqual(self.submit(token="")[0], 401)
        self.assertEqual(self.submit(token="wrong")[0], 401)
        self.assertEqual(self.submit(token=self.cfg.client_token)[0], 401)
        self.setup_admin()
        self.assertEqual(self.submit(token="")[0], 401)
        self.assertEqual(self.store.dashboard()["accounts"], [])
        token = self.store.setting("key_import_token")
        self.assertNotEqual(token, self.cfg.client_token)
        self.assertEqual(self.request("/v1/models", headers={"Authorization": "Bearer " + token})[0], 401)
        self.assertEqual(self.request("/admin/api/state", auth=False,
                                      headers={"Authorization": "Bearer " + token})[0], 401)

    def test_adds_keys_before_browser_setup_and_requests_use_them_immediately(self):
        key = "offline-received-key-1001"
        status, result, headers = self.submit({"api_key": key, "name": "第二账户"})
        self.assertEqual(status, 201)
        self.assertTrue(result["created"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["name"], "第二账户")
        self.assertEqual(result["key_mask"], "•••• 1001")
        self.assertNotIn(key, json.dumps(result))
        self.assertNotIn("api_key", result)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.store.setting("password"), "")
        self.assertFalse(self.gateway.calls)
        self.assertEqual(self.request("/v1/chat/completions", self.payload)[0], 200)
        self.assertEqual(self.gateway.calls, [("POST", key)])
        reopened = Store(self.directory.name)
        self.assertEqual(reopened.account(result["id"])["api_key"], key)
        self.assertEqual(reopened.dashboard()["accounts"][0]["name"], "第二账户")

    def test_missing_name_generates_unique_names_and_optional_disabled_account(self):
        _, first, _ = self.submit()
        status, second, _ = self.submit({"api_key": "offline-received-key-2002", "enabled": False})
        self.assertEqual(status, 201)
        self.assertEqual(first["name"], "API 账户 %s" % first["id"])
        self.assertNotEqual(first["name"], second["name"])
        self.assertFalse(second["enabled"])

    def test_duplicate_is_idempotent_and_preserves_account_state_and_stats(self):
        key = "offline-received-key-1001"
        _, initial, _ = self.submit({"api_key": key, "name": "原名称", "enabled": False})
        account = self.store.account(initial["id"])
        self.store.record(account, "gpt-6-luna", 429, 30)
        before = self.store.account(initial["id"])
        status, duplicate, _ = self.submit({"api_key": key, "name": "忽略新名称", "enabled": True})
        self.assertEqual(status, 200)
        self.assertFalse(duplicate["created"])
        self.assertFalse(duplicate["enabled"])
        self.assertEqual(duplicate["name"], "原名称")
        self.assertEqual(duplicate["id"], initial["id"])
        self.assertEqual(self.store.account(initial["id"]), before)

    def test_parallel_retries_create_only_one_account(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            replies = list(executor.map(lambda _: self.submit(), range(6)))
        self.assertEqual(sorted(status for status, _, _ in replies), [200] * 5 + [201])
        self.assertEqual(len({result["id"] for _, result, _ in replies}), 1)
        self.assertEqual(len(self.store.dashboard()["accounts"]), 1)

    def test_validation_body_limit_and_post_only(self):
        invalid = [{}, {"api_key": ""}, {"api_key": None}, {"api_key": ["key"]},
                   {"api_key": "has spaces"}, {"api_key": "key\r\n"}, {"api_key": "key", "enabled": "false"},
                   {"api_key": "key", "name": ""}, {"api_key": "key", "name": "x" * 81},
                   {"api_key": "key", "unexpected": "never echo this"}, ["not an object"]]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.submit(body)[0], 400)
        self.assertEqual(self.submit({"api_key": "x" * 17000})[0], 413)
        self.assertEqual(self.submit(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.submit(headers={"Transfer-Encoding": "chunked"})[0], 400)
        for method in ("GET", "OPTIONS"):
            status, _, headers = self.request("/api/keys", method=method)
            self.assertEqual(status, 405)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertFalse(self.store.dashboard()["accounts"])

    def test_account_limit_allows_idempotent_retry_but_rejects_new_key(self):
        for index in range(50):
            self.store.receive_key({"api_key": "offline-key-%s" % index})
        self.assertEqual(self.submit({"api_key": "offline-key-0"})[0], 200)
        self.assertEqual(self.submit({"api_key": "offline-key-51"})[0], 400)
        self.assertEqual(len(self.store.dashboard()["accounts"]), 50)

    def test_does_not_log_keys_tokens_or_rejected_query_parameters(self):
        key, token = "offline-log-secret-1001", self.store.setting("key_import_token")
        logs = io.StringIO()
        with patch.dict(os.environ, {"NUTRIENT2API_QUIET": ""}), patch("sys.stderr", logs):
            self.assertEqual(self.submit({"api_key": key})[0], 201)
            status, result, _ = self.request("/api/keys?api_key=do-not-log-this&token=also-secret",
                                            {"api_key": key}, headers={"Authorization": "Bearer " + token})
        self.assertEqual(status, 400)
        self.assertIn("POST /api/keys", logs.getvalue())
        for secret in (key, token, "do-not-log-this", "also-secret"):
            self.assertNotIn(secret, logs.getvalue())
            self.assertNotIn(secret, json.dumps(result))

    def test_cli_tokens_persist_and_rotation_revokes_existing_token_without_restart(self):
        token = self.cli("key-import-token")
        self.assertEqual(token, self.store.setting("key_import_token"))
        self.assertEqual(token, self.cli("key-import-token"))
        self.assertEqual(self.cli("client-token"), self.cfg.client_token)
        self.assertEqual(self.submit(token=token)[0], 201)
        replacement = self.cli("rotate-key-import-token")
        self.assertNotEqual(replacement, token)
        self.assertEqual(self.submit(token=token)[0], 401)
        self.assertEqual(self.submit(token=replacement)[0], 200)
        self.assertEqual(self.store.setting("client_token"), self.cfg.client_token)

    def test_upgrade_creates_import_token_without_overwriting_existing_config(self):
        self.setup_admin()
        self.submit()
        names = ("password", "auth_version", "client_token", "setup_code")
        original = {key: self.store.setting(key) for key in names}
        original_accounts = self.store.dashboard()["accounts"]
        with self.store.transaction() as db:
            db.execute("DELETE FROM settings WHERE key='key_import_token'")
        reopened = Store(self.directory.name)
        self.assertTrue(reopened.setting("key_import_token").startswith("n2k_"))
        self.assertEqual({key: reopened.setting(key) for key in names}, original)
        self.assertEqual(reopened.dashboard()["accounts"], original_accounts)


if __name__ == "__main__":
    unittest.main()
