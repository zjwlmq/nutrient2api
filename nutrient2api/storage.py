"""Small, persistent control plane. No prompts or completion text are stored."""

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return salt + ":" + digest


def password_matches(password, encoded):
    if not encoded or not isinstance(password, str) or len(password) > 256:
        return False
    salt, expected = encoded.split(":", 1)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return secrets.compare_digest(actual, expected)


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("管理员密码需要 12–256 个字符")


class Store:
    def __init__(self, directory, api_key="", client_token=""):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "control.db"
        self.lock = threading.RLock()
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, api_key TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1, last_status INTEGER NOT NULL DEFAULT 0,
                    last_checked REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0,
                    last_selected REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                    account_name TEXT NOT NULL, model TEXT NOT NULL, status INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL, streaming INTEGER NOT NULL
                );
            """)
            # executescript commits its own transaction; protect the one-time import.
            db.execute("BEGIN IMMEDIATE")
            if not self.get(db, "initialized"):
                defaults = {"initialized": "1", "password": "", "auth_version": secrets.token_hex(16),
                            "setup_code": secrets.token_urlsafe(24), "total_requests": "0", "total_successes": "0",
                            "client_token": client_token or "n2a_" + secrets.token_urlsafe(32)}
                for key, value in defaults.items():
                    self.put(db, key, value)
                if api_key:
                    db.execute("INSERT INTO accounts(name,api_key,created_at) VALUES (?,?,?)",
                               ("原 .env 账户", api_key, time.time()))
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
            os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(str(self.path), timeout=15)
            db.row_factory = sqlite3.Row
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def get(db, key):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, str(value)))

    def setting(self, key):
        with self.transaction() as db:
            return self.get(db, key)

    def setup(self, code, password):
        validate_password(password)
        encoded = password_hash(password)
        with self.transaction() as db:
            expected = self.get(db, "setup_code")
            if self.get(db, "password") or not expected or not secrets.compare_digest(code.encode(), expected.encode()):
                raise ValueError("初始化码无效，或初始化已经完成")
            self.put(db, "password", encoded)
            self.put(db, "setup_code", "")
            version = secrets.token_hex(16)
            self.put(db, "auth_version", version)
            return version

    def authenticate(self, password):
        with self.transaction() as db:
            encoded, version = self.get(db, "password"), self.get(db, "auth_version")
        return version if password_matches(password, encoded) else None

    def change_password(self, old, new):
        validate_password(new)
        encoded = password_hash(new)
        with self.transaction() as db:
            if not password_matches(old, self.get(db, "password")):
                raise ValueError("当前密码不正确")
            self.put(db, "password", encoded)
            version = secrets.token_hex(16)
            self.put(db, "auth_version", version)
            return version

    def reset_admin(self):
        code = secrets.token_urlsafe(24)
        with self.transaction() as db:
            self.put(db, "password", "")
            self.put(db, "auth_version", secrets.token_hex(16))
            self.put(db, "setup_code", code)
        return code

    def rotate_token(self):
        token = "n2a_" + secrets.token_urlsafe(32)
        with self.transaction() as db:
            self.put(db, "client_token", token)
        return token

    def save_account(self, body, account_id=None):
        name = body.get("name", "")
        key = body.get("api_key", "")
        enabled = body.get("enabled", True)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("账户名称需要 1–80 个字符")
        if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("Key 只能包含可打印的英文字符，且不能含空格")
        if not isinstance(enabled, bool):
            raise ValueError("启用状态无效")
        with self.transaction() as db:
            current = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone() if account_id else None
            if account_id and not current:
                raise ValueError("账户不存在")
            if not current and not key:
                raise ValueError("请填写 Nutrient API Key")
            try:
                if current:
                    changed = bool(key and key != current["api_key"])
                    db.execute("UPDATE accounts SET name=?,api_key=?,enabled=?,last_status=?,last_checked=?,cooldown_until=? WHERE id=?",
                               (name.strip(), key or current["api_key"], int(enabled),
                                0 if changed else current["last_status"], 0 if changed else current["last_checked"],
                                0 if changed else current["cooldown_until"], account_id))
                else:
                    if db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] >= 50:
                        raise ValueError("最多可保存 50 个账户")
                    account_id = db.execute("INSERT INTO accounts(name,api_key,enabled,created_at) VALUES (?,?,?,?)",
                                            (name.strip(), key, int(enabled), time.time())).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("这个 Key 已经添加过了") from None
        return account_id

    def account(self, account_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise ValueError("账户不存在")
            return dict(row)

    def delete_account(self, account_id):
        with self.transaction() as db:
            if not db.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount:
                raise ValueError("账户不存在")

    def select_account(self, excluded):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM accounts WHERE enabled=1 AND cooldown_until<=? ORDER BY last_selected,id", (time.time(),))
            row = next((r for r in rows if r["id"] not in excluded), None)
            if row is None:
                return None
            db.execute("UPDATE accounts SET last_selected=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def record(self, account, model, status, duration_ms, streaming=False, probe=False):
        now = time.time()
        cooldown = now + (60 if status == 429 else 300) if status in (401, 403, 429) else 0
        with self.transaction() as db:
            # A slow request for a replaced key must not change the new key's health.
            db.execute("UPDATE accounts SET last_status=?,last_checked=?,cooldown_until=? WHERE id=? AND api_key=?",
                       (status, now, cooldown, account["id"], account["api_key"]))
            if probe:
                return
            success = int(200 <= status < 300)
            db.execute("UPDATE accounts SET requests=requests+1,successes=successes+? WHERE id=?", (success, account["id"]))
            self.put(db, "total_requests", int(self.get(db, "total_requests")) + 1)
            self.put(db, "total_successes", int(self.get(db, "total_successes")) + success)
            db.execute("INSERT INTO requests(created_at,account_name,model,status,duration_ms,streaming) VALUES (?,?,?,?,?,?)",
                       (now, account["name"], model, status, max(0, duration_ms), int(streaming)))
            db.execute("DELETE FROM requests WHERE id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT 200)")

    def dashboard(self):
        with self.transaction() as db:
            accounts = []
            for row in db.execute("SELECT * FROM accounts ORDER BY id"):
                item = dict(row)
                key = item.pop("api_key")
                item["key_mask"] = "•••• " + (key[-4:] if len(key) > 8 else "••••")
                item["enabled"] = bool(item["enabled"])
                accounts.append(item)
            return {"accounts": accounts,
                    "requests": [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 200")],
                    "total_requests": int(self.get(db, "total_requests")),
                    "total_successes": int(self.get(db, "total_successes"))}
