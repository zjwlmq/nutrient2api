"""Stdlib HTTP server exposing an OpenAI-compatible surface."""

import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import catalog, upstream
from .admin import Admin, security_headers
from .pool import AccountPool
from .prompt import PromptPolicy
from .storage import Store

ROUTES = {
    "/v1/chat/completions": "chat",
    "/chat/completions": "chat",
    "/v1/embeddings": "embeddings",
    "/embeddings": "embeddings",
}


class Config:
    def __init__(self) -> None:
        self.api_key = os.environ.get("NUTRIENT_API_KEY", "").strip()
        self.base_url = os.environ.get("NUTRIENT_BASE_URL", upstream.DEFAULT_BASE).rstrip("/")
        self.host = os.environ.get("NUTRIENT2API_HOST", "127.0.0.1")
        self.port = int(os.environ.get("NUTRIENT2API_PORT", "8787"))
        # Legacy env values are imported once into persistent storage.
        self.client_token = os.environ.get("NUTRIENT2API_TOKEN", "").strip()
        self.data_dir = os.environ.get("NUTRIENT2API_DATA_DIR", "./data")
        self.max_body_bytes = int(os.environ.get("NUTRIENT2API_MAX_BODY_BYTES", "10485760"))
        self.client_timeout = float(os.environ.get("NUTRIENT2API_CLIENT_TIMEOUT", "30"))
        self.max_connections = int(os.environ.get("NUTRIENT2API_MAX_CONNECTIONS", "32"))
        if min(self.max_body_bytes, self.client_timeout, self.max_connections) <= 0:
            raise ValueError("request limits must be positive")
        self.policy = PromptPolicy.from_env()


class Handler(BaseHTTPRequestHandler):
    server_version = "nutrient2api/1.2"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.cfg.client_timeout)  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("NUTRIENT2API_QUIET"):
            return
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def log_request(self, code="-", size="-") -> None:
        if urlparse(getattr(self, "path", "")).path.rstrip("/") == "/api/keys":
            # Even mistakenly supplied URL parameters must not reach access logs.
            self.log_message('"%s /api/keys %s" %s %s', self.command, self.request_version, code, size)
        else:
            super().log_request(code, size)

    def _send_json(self, status: int, payload: Any) -> None:
        # In particular, never reuse a connection whose rejected body is unread.
        self.close_connection = True
        blob = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Connection", "close")
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/admin") or path == "/api/keys":
            security_headers(self)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(blob)

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": kind, "code": None}})

    def _authorized(self, token_setting="client_token") -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = self.server.store.setting(token_setting)
        return bool(expected) and secrets.compare_digest(token.encode(), expected.encode())

    def _read_json(self, max_bytes=None) -> Optional[Dict[str, Any]]:
        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "chunked request bodies are not supported; send Content-Length")
            return None
        if not lengths:
            self._error(411, "Content-Length is required")
            return None
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(400, "invalid Content-Length")
            return None
        try:
            length = int(lengths[0])
        except ValueError:
            self._error(400, "invalid Content-Length")
            return None
        if length > min(cfg.max_body_bytes, max_bytes or cfg.max_body_bytes):
            self._error(413, "request body exceeds configured size limit")
            return None
        if self.headers.get_content_type() != "application/json":
            self._error(415, "Content-Type must be application/json")
            return None
        try:
            raw = self.rfile.read(length)
        except (socket.timeout, TimeoutError):
            self._error(408, "timed out reading request body")
            return None
        if len(raw) != length:
            self._error(400, "incomplete request body")
            return None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            self._error(400, "request body is not valid JSON")
            return None
        if not isinstance(body, dict):
            self._error(400, "request body must be a JSON object")
            return None
        return body

    # -- verbs ------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/admin") or path == "/api/keys":
            self._error(405, "method not allowed")
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/keys":
            self._error(405, "use POST with a JSON body")
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/admin/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/admin" or path.startswith("/admin/"):
            self._handle_admin(path)
            return
        if path in ("/health", "/healthz"):
            cfg: Config = self.server.cfg  # type: ignore[attr-defined]
            self._send_json(200, {"status": "ok", "upstream": cfg.base_url})
            return
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                self._error(401, "invalid api key", "authentication_error")
                return
            self._handle_models()
            return
        self._error(404, f"unknown path {path}", "not_found")

    def do_POST(self) -> None:
        self.close_connection = True
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/keys":
            self._handle_key_receive()
            return
        if path.startswith("/admin/"):
            self._handle_admin(path)
            return
        action = ROUTES.get(path)
        if not action:
            self._error(404, f"unknown path {path}", "not_found")
            return
        if not self._authorized():
            self._error(401, "invalid api key", "authentication_error")
            return

        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        body = self._read_json()
        if body is None:
            return

        if not isinstance(body.get("model"), str):
            self._error(400, "model must be a string")
            return
        target = catalog.resolve(body["model"])
        if target is None:
            self._error(
                404,
                f"model `{body.get('model')}` not found; available: "
                + ", ".join(sorted(catalog.ROUTES)),
                "model_not_found",
            )
            return

        if action != target.api:
            self._error(400, "model does not support this endpoint")
            return
        if action == "chat":
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages or not all(
                isinstance(m, dict) and isinstance(m.get("role"), str) for m in messages
            ):
                self._error(400, "messages must be a non-empty array of message objects")
                return
            if "stream" in body and not isinstance(body["stream"], bool):
                self._error(400, "stream must be a boolean")
                return
            if "stream_options" in body and not isinstance(body["stream_options"], dict):
                self._error(400, "stream_options must be an object")
                return

        try:
            if action == "chat":
                self._handle_chat(cfg, target, body)
            elif action == "embeddings":
                self._handle_embeddings(cfg, target, body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except upstream.UpstreamError as exc:
            status = exc.status if 400 <= exc.status < 600 else 502
            self._send_json(status, exc.payload)
        except Exception as exc:  # keep the surface alive on unexpected faults
            self.log_message("proxy failure: %s", type(exc).__name__)
            self._error(500, "internal proxy error", "api_error")

    # -- handlers ---------------------------------------------------------
    def _handle_key_receive(self):
        if not self._authorized("key_import_token"):
            self._error(401, "invalid key import token", "authentication_error")
            return
        if urlparse(self.path).query:
            self._error(400, "send credentials in the Authorization header and JSON body, not URL parameters")
            return
        body = self._read_json(max_bytes=16384)
        if body is None:
            return
        try:
            result = self.server.store.receive_key(body)
            self._send_json(201 if result["created"] else 200, result)
        except ValueError as exc:
            self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_message("key import failure: %s", type(exc).__name__)
            self._error(500, "unable to save key", "api_error")

    def _handle_admin(self, path):
        try:
            self.server.admin.handle(self, path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_message("admin failure: %s", type(exc).__name__)
            self._error(500, "管理后台暂时不可用，请查看服务日志")

    def _handle_models(self) -> None:
        data = [
            {
                "id": m.id,
                "object": "model",
                "created": 0,
                "owned_by": "nutrient",
                "context_window": m.context_window,
                "max_output_tokens": m.max_output,
            }
            for m in catalog.list_models()
        ]
        self._send_json(200, {"object": "list", "data": data})

    def _handle_chat(self, cfg: Config, target: "catalog.Model", body: Dict[str, Any]) -> None:
        if body.get("stream"):
            self._stream_chat(cfg, target, body)
            return
        result = self.server.pool.call(target.id, lambda key: upstream.chat(
            key, target.upstream, body, cfg.base_url, cfg.policy, retries=0))
        result["model"] = target.id
        self._send_json(200, result)

    def _stream_chat(self, cfg: Config, target: "catalog.Model", body: Dict[str, Any]) -> None:
        events = self.server.pool.stream(target.id, lambda key: upstream.chat_stream(
            key, target.upstream, body, cfg.base_url, cfg.policy, retries=0))
        started = False
        try:
            # Open upstream and obtain a complete event before committing HTTP 200.
            first = next(events, None)
            if first is None:
                raise upstream.UpstreamError(502, {"error": {"message": "empty upstream stream"}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            self._write_chunk(_stream_model(first, target.id).encode())
            for event in events:
                self._write_chunk(_stream_model(event, target.id).encode())
            self._write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:
            if not started:
                raise
            # Once streaming has begun, report errors inside SSE, never as new headers.
            error = exc.payload if isinstance(exc, upstream.UpstreamError) else {
                "error": {"message": "upstream stream failed", "type": "api_error"}
            }
            try:
                self._write_chunk(upstream.encode_sse(error).encode())
                self._write_chunk(b"data: [DONE]\n\n")
                self._write_chunk(b"")
            except OSError:
                pass
        finally:
            events.close()

    def _write_chunk(self, blob: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(blob), blob))
        self.wfile.flush()

    def _handle_embeddings(self, cfg: Config, target: "catalog.Model", body: Dict[str, Any]) -> None:
        result = self.server.pool.call(target.id, lambda key: upstream.embeddings(
            key, target.upstream, body, cfg.base_url, retries=0))
        if isinstance(result, dict):
            result["model"] = target.id
        self._send_json(200, result)


def _stream_model(event: str, model: str) -> str:
    data = upstream.event_data(event)
    if not data or data == "[DONE]":
        return event
    try:
        payload = json.loads(data)
    except ValueError:
        return event
    if isinstance(payload, dict) and "model" in payload:
        payload["model"] = model
        return upstream.encode_sse(payload)
    return event


class ProxyHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64

    def __init__(self, address: Any, cfg: Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.data_dir, cfg.api_key, cfg.client_token)
        cfg.client_token = self.store.setting("client_token")
        self.admin = Admin(self.store, cfg)
        self.pool = AccountPool(self.store)
        self._slots = threading.BoundedSemaphore(cfg.max_connections)
        super().__init__(address, Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Retry-After: 1\r\nConnection: close\r\n\r\n"
                )
                # A close with unread bytes can reset the socket before the
                # client receives 503. Half-close, then drain a bounded amount.
                request.shutdown(socket.SHUT_WR)
                request.settimeout(0.05)
                remaining = 8192
                while remaining:
                    data = request.recv(min(4096, remaining))
                    if not data:
                        break
                    remaining -= len(data)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(cfg: Optional[Config] = None) -> None:
    cfg = cfg or Config()
    with ProxyHTTPServer((cfg.host, cfg.port), cfg) as httpd:
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(
                signal.SIGTERM,
                lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start(),
            )
        print(f"nutrient2api listening on http://{cfg.host}:{cfg.port}/v1  -> {cfg.base_url}", flush=True)
        print("Admin console: /admin/", flush=True)
        setup_code = httpd.store.setting("setup_code")
        if setup_code:
            print("INITIAL SETUP CODE: " + setup_code, flush=True)
            print("Open /admin/ and use this one-time code to create your administrator password.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
