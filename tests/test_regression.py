"""Offline deployment regressions: real loopback HTTP, no paid upstream calls."""

import http.client
import io
import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from nutrient2api import server, upstream
from nutrient2api.prompt import PromptPolicy


def completion_chunk(text, index=0, finish=None):
    return {
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
        "model": "nutrient-s",
        "choices": [{"index": index, "delta": {"content": text}, "finish_reason": finish}],
    }


def sse(payload):
    # Spaced JSON deliberately exercises model mapping independently of encoding.
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def parse_sse(raw):
    text = raw.decode()
    if not text.endswith("\n\n"):
        raise AssertionError("missing final SSE event separator")
    events = []
    for frame in text.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
        if data:
            events.append(data if data == "[DONE]" else json.loads(data))
    return events


class MockGateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body))
        mode = self.server.mode
        status, content_type = 200, "application/json"
        if mode == "rate_limit":
            status = 429
            data = json.dumps({"error": {"message": "rate limited"}}).encode()
        elif mode == "conflict" and self.path.endswith("/chat/completions"):
            status = 400
            data = json.dumps({"error": {"message": "Function tools with reasoning_effort are not supported"}}).encode()
        elif self.path.endswith("/responses"):
            if any(item.get("type") == "function_call_output" for item in body["input"]):
                output = [{"type": "message", "content": [{"type": "output_text", "text": "sunny"}]}]
            else:
                output = [{"type": "function_call", "call_id": "call_1", "name": "weather", "arguments": "{}"}]
            data = json.dumps({"id": "resp-test", "model": "nutrient-s", "created_at": 1, "output": output,
                               "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}).encode()
        elif body.get("stream"):
            if mode == "wrong_type":
                data = b'{}'
            else:
                content_type = "text/event-stream"
                data = b"".join(sse(event) for event in self.server.events)
                if mode != "truncated":
                    data += b"data: [DONE]\n\n"
        elif self.path.endswith("/embeddings"):
            data = json.dumps({"object": "list", "data": [{"embedding": [0.1, 0.2]}]}).encode()
        else:
            data = json.dumps({"id": "chatcmpl-test", "model": "nutrient-s", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
            ]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class HTTPRegression(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"NUTRIENT2API_QUIET": "1"}, clear=True)
        self.env.start()
        self.cfg = server.Config()
        self.cfg.api_key = "offline-upstream-key"
        self.cfg.client_token = "offline-client-token"
        self.cfg.max_connections = 1
        self.cfg.client_timeout = 1
        self.cfg.policy = PromptPolicy()
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), MockGateway)
        self.gateway.mode = "ok"
        self.gateway.requests = []
        self.gateway.events = [completion_chunk("hello"), completion_chunk("", finish="stop")]
        self.cfg.base_url = "http://127.0.0.1:%s/v1" % self.gateway.server_port
        self.proxy = server.ProxyHTTPServer(("127.0.0.1", 0), self.cfg)
        self.threads = []
        for httpd in (self.gateway, self.proxy):
            worker = threading.Thread(target=lambda s=httpd: s.serve_forever(poll_interval=0.01))
            worker.start()
            self.threads.append(worker)
        self.payload = {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "hello"}]}

    def tearDown(self):
        for httpd in (self.proxy, self.gateway):
            httpd.shutdown()
            httpd.server_close()
        for worker in self.threads:
            worker.join(3)
        self.env.stop()

    def request(self, path="/v1/chat/completions", payload=None, method="POST", auth=True, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=4)
        request_headers = {"Content-Type": "application/json", "Connection": "close"}
        if auth:
            request_headers["Authorization"] = "Bearer " + self.cfg.client_token
        request_headers.update(headers or {})
        data = raw if raw is not None else json.dumps(self.payload if payload is None else payload).encode()
        try:
            conn.request(method, path, body=data if method == "POST" else None, headers=request_headers)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def test_health_is_public_and_models_require_token(self):
        self.assertEqual(self.request("/health", method="GET", auth=False)[0], 200)
        self.assertEqual(self.request("/v1/models", method="GET", auth=False)[0], 401)
        self.assertEqual(self.request("/v1/models", method="GET")[0], 200)
        self.assertFalse(self.gateway.requests)

    def test_unauthorized_chat_never_calls_upstream(self):
        self.assertEqual(self.request(auth=False)[0], 401)
        self.assertFalse(self.gateway.requests)

    def test_chat_alias_and_token_limit(self):
        status, body, _ = self.request(payload=dict(self.payload, max_tokens=20, max_completion_tokens=30))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"], "gpt-6-luna")
        sent = self.gateway.requests[0][1]
        self.assertEqual(sent["model"], "nutrient-s")
        self.assertEqual(sent["max_completion_tokens"], 30)
        self.assertNotIn("max_tokens", sent)

    def test_embeddings_and_wrong_endpoint(self):
        status, body, _ = self.request("/v1/embeddings", {"model": "text-embedding-3-small", "input": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"], "text-embedding-3-small")
        self.assertEqual(self.request("/v1/embeddings")[0], 400)

    def test_stream_frames_models_and_done(self):
        status, raw, headers = self.request(payload=dict(self.payload, stream=True))
        events = parse_sse(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Accel-Buffering"], "no")
        self.assertEqual(events[-1], "[DONE]")
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "hello")
        self.assertTrue(all(e["model"] == "gpt-6-luna" for e in events[:-1]))

    def test_literal_done_does_not_end_stream(self):
        self.gateway.events = [completion_chunk("literal [DONE]"), completion_chunk("tail", finish="stop")]
        events = parse_sse(self.request(payload=dict(self.payload, stream=True))[1])
        self.assertEqual(events[1]["choices"][0]["delta"]["content"], "tail")

    def test_stream_http_error_stays_a_single_json_error(self):
        self.gateway.mode = "rate_limit"
        status, body, headers = self.request(payload=dict(self.payload, stream=True))
        self.assertEqual(status, 429)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body)["error"]["message"], "rate limited")
        self.assertEqual(len(self.gateway.requests), 1)

    def test_wrong_upstream_content_type_before_headers(self):
        self.gateway.mode = "wrong_type"
        status, body, _ = self.request(payload=dict(self.payload, stream=True))
        self.assertEqual(status, 502)
        self.assertIn("error", json.loads(body))

    def test_midstream_failure_uses_sse_error(self):
        self.gateway.mode = "truncated"
        status, body, _ = self.request(payload=dict(self.payload, stream=True))
        events = parse_sse(body)
        self.assertEqual(status, 200)
        self.assertIn("error", events[-2])
        self.assertEqual(events[-1], "[DONE]")
        self.assertNotIn(b"HTTP/1.1", body)

    def test_scrubber_flushes_before_finish_and_preserves_usage(self):
        self.cfg.policy = PromptPolicy(suppress_claim=True)
        self.gateway.events.append({"id": "chatcmpl-test", "model": "nutrient-s", "choices": [], "usage": {"total_tokens": 10}})
        events = parse_sse(self.request(payload=dict(self.payload, stream=True))[1])
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "hello")
        self.assertEqual(events[0]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(events[0]["id"], "chatcmpl-test")
        self.assertEqual(events[1]["usage"]["total_tokens"], 10)

    def test_scrubber_keeps_choices_separate(self):
        self.cfg.policy = PromptPolicy(suppress_claim=True)
        self.gateway.events = [completion_chunk("first", 0), completion_chunk("second", 1),
                               completion_chunk("", 0, "stop"), completion_chunk("", 1, "stop")]
        events = parse_sse(self.request(payload=dict(self.payload, stream=True))[1])
        results = {e["choices"][0]["index"]: e["choices"][0]["delta"]["content"] for e in events[:-1]}
        self.assertEqual(results, {0: "first", 1: "second"})

    def test_tool_round_trip_and_store_false_survive_fallback(self):
        self.gateway.mode = "conflict"
        tools = [{"type": "function", "function": {"name": "weather", "parameters": {}, "strict": False}}]
        payload = dict(self.payload, tools=tools, tool_choice={"type": "function", "function": {"name": "weather"}},
                       reasoning_effort="low", store=False)
        status, body, _ = self.request(payload=payload)
        self.assertEqual(status, 200)
        reply = json.loads(body)["choices"][0]["message"]
        followup = dict(payload, messages=payload["messages"] + [reply, {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}])
        status, body, _ = self.request(payload=followup)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "sunny")
        sent = self.gateway.requests[-1][1]
        self.assertIs(sent["store"], False)
        self.assertIs(sent["tools"][0]["strict"], False)
        self.assertEqual(sent["tool_choice"], {"type": "function", "name": "weather"})
        self.assertEqual(sent["input"][1]["type"], "function_call")
        self.assertEqual(sent["input"][2], {"type": "function_call_output", "call_id": "call_1", "output": "sunny"})

    def test_stream_tool_fallback_has_calls_usage_and_terminator(self):
        self.gateway.mode = "conflict"
        status, raw, _ = self.request(payload=dict(self.payload, stream=True, store=False, reasoning_effort="low",
                                                  stream_options={"include_usage": True}))
        self.assertEqual(status, 200)
        events = parse_sse(raw)
        self.assertEqual(events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "weather")
        self.assertEqual(events[1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(events[2]["usage"]["total_tokens"], 10)
        self.assertEqual(events[-1], "[DONE]")
        self.assertIs(self.gateway.requests[-1][1]["store"], False)

    def test_invalid_bodies_return_json_errors(self):
        for raw in (b"[]", b"null", b"false", b"3", b"broken-json", b"\xff", b"[" * 2000 + b"]" * 2000):
            with self.subTest(raw=raw):
                status, body, _ = self.request(raw=raw)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertFalse(self.gateway.requests)

    def test_content_length_validation_and_body_size(self):
        for length in ("-1", "nonsense"):
            with self.subTest(length=length):
                self.assertEqual(self.request(raw=b"{}", headers={"Content-Length": length})[0], 400)
        self.cfg.max_body_bytes = 4
        self.assertEqual(self.request(raw=b"12345")[0], 413)
        self.assertFalse(self.gateway.requests)

    def test_unsupported_transfer_and_content_type(self):
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertFalse(self.gateway.requests)

    def test_invalid_chat_shape_and_unknown_model(self):
        for payload in ({"model": 1}, dict(self.payload, messages="bad"), dict(self.payload, stream="true"),
                        dict(self.payload, stream_options="bad")):
            self.assertEqual(self.request(payload=payload)[0], 400)
        self.assertEqual(self.request(payload=dict(self.payload, model="unknown"))[0], 404)
        self.assertEqual(self.request("/v1/responses")[0], 404)
        self.assertFalse(self.gateway.requests)

    def test_incomplete_body_times_out(self):
        self.cfg.client_timeout = 0.1
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=3)
        try:
            conn.putrequest("POST", "/v1/chat/completions")
            conn.putheader("Authorization", "Bearer " + self.cfg.client_token)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "10")
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 408)
            self.assertIn("error", json.loads(response.read()))
        finally:
            conn.close()

    def test_connection_limit_rejects_extra_client_and_recovers(self):
        data = json.dumps(self.payload).encode()
        conn = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=3)
        try:
            header = ("POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                      "Authorization: Bearer %s\r\nContent-Type: application/json\r\n"
                      "Content-Length: %s\r\nExpect: 100-continue\r\nConnection: close\r\n\r\n") % (self.cfg.client_token, len(data))
            conn.sendall(header.encode())
            interim = conn.recv(4096)
            self.assertIn(b"100 Continue", interim)
            self.assertEqual(self.request("/health", method="GET")[0], 503)
            conn.sendall(data)
            chunks = []
            while True:
                part = conn.recv(4096)
                if not part:
                    break
                chunks.append(part)
            self.assertIn(b"200 OK", b"".join(chunks))
        finally:
            conn.close()
        self.assertEqual(self.request("/health", method="GET")[0], 200)


class ConversionRegression(unittest.TestCase):
    def test_done_does_not_wait_for_upstream_eof(self):
        class StaysOpen:
            def readline(self, size):
                if hasattr(self, "read_once"):
                    raise AssertionError("read after [DONE]")
                self.read_once = True
                return b"data: [DONE]\n"
        self.assertEqual(list(upstream._sse_events(StaysOpen())), ["data: [DONE]\n\n"])

    def test_assistant_history_and_image_detail(self):
        items = upstream._responses_input([
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/image.png", "detail": "low"}}]},
        ])
        self.assertEqual(items[0]["content"], "previous answer")
        self.assertEqual(items[1]["content"][0]["detail"], "low")

    def test_incomplete_response_and_usage_details(self):
        result = upstream._responses_to_chat({"output": [], "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                               "usage": {"output_tokens_details": {"reasoning_tokens": 5}}}, "nutrient-s")
        self.assertEqual(result["choices"][0]["finish_reason"], "length")
        self.assertEqual(result["usage"]["completion_tokens_details"]["reasoning_tokens"], 5)

    def test_transport_timeout_does_not_replay_generation(self):
        for failure in (socket.timeout("offline"), http.client.IncompleteRead(b"partial", 10)):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(upstream.urllib.request, "urlopen", side_effect=failure) as call:
                    with self.assertRaises(upstream.UpstreamError) as raised:
                        upstream._request("http://127.0.0.1", "offline-key", {"messages": []})
                    self.assertEqual(raised.exception.status, 504)
                    self.assertEqual(call.call_count, 1)

    def test_malformed_upstream_json_becomes_bad_gateway(self):
        for raw in (b"not-json", b"[]"):
            with self.subTest(raw=raw):
                with patch.object(upstream.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
                    with self.assertRaises(upstream.UpstreamError) as raised:
                        upstream._request("http://127.0.0.1", "offline-key", {"messages": []})
                    self.assertEqual(raised.exception.status, 502)

    def test_public_bind_requires_token_before_listening(self):
        with patch.dict(os.environ, {"NUTRIENT_API_KEY": "offline-key", "NUTRIENT2API_HOST": "0.0.0.0"}, clear=True):
            with self.assertRaisesRegex(SystemExit, "NUTRIENT2API_TOKEN"):
                server.serve()


if __name__ == "__main__":
    unittest.main()
