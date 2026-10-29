"""End-to-end check against the running HTTP proxy, using the OpenAI SDK wire format."""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("NUTRIENT2API_TEST_BASE", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("NUTRIENT2API_TOKEN", "local")


def post(path, payload, stream=False):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    with resp:
        if stream:
            raw = resp.read().decode()
            assert raw.endswith("\n\n"), "missing SSE event separator"
            events = [event for event in raw.split("\n\n") if event.startswith("data:")]
            for event in events:
                data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if data != "[DONE]":
                    parsed = json.loads(data)
                    assert "error" not in parsed, parsed
            return resp.status, events
        return resp.status, json.loads(resp.read())


def get(path):
    req = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.loads(r.read())


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
        return True
    except Exception as exc:
        print(f"FAIL  {name}: {exc}")
        return False


def models():
    st, body = get("/v1/models")
    ids = [m["id"] for m in body["data"]]
    assert st == 200 and "gpt-6-luna" in ids, body


def chat_basic():
    st, body = post(
        "/v1/chat/completions",
        {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "reply exactly: PROXY OK"}], "max_tokens": 2000},
    )
    assert st == 200, body
    assert body["model"] == "gpt-6-luna", body["model"]
    assert body["choices"][0]["message"]["content"].strip(), body


def chat_alias_nutrient():
    # caller may still pass the raw gateway alias
    st, body = post(
        "/v1/chat/completions",
        {"model": "nutrient-m", "messages": [{"role": "user", "content": "one word"}], "max_tokens": 2000},
    )
    assert st == 200, body
    assert body["model"] == "gpt-6.1-sol", body["model"]


def chat_stream():
    st, lines = post(
        "/v1/chat/completions",
        {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "count 1 2 3"}], "max_tokens": 2000, "stream": True},
        stream=True,
    )
    assert st == 200, lines
    assert any("[DONE]" in l for l in lines), lines[-3:]
    assert any("chat.completion.chunk" in l for l in lines), lines[:2]


def embeddings():
    st, body = post("/v1/embeddings", {"model": "text-embedding-3-small", "input": "hello", "dimensions": 512})
    assert st == 200, body
    assert len(body["data"][0]["embedding"]) == 512
    assert body["model"] == "text-embedding-3-small", body["model"]


def bad_model():
    st, body = post("/v1/chat/completions", {"model": "gpt-9-turbo", "messages": [{"role": "user", "content": "x"}]})
    assert st == 404, (st, body)


def tools():
    st, body = post(
        "/v1/chat/completions",
        {
            "model": "gpt-6-luna",
            "messages": [{"role": "user", "content": "Weather in Berlin? Use the tool."}],
            "max_tokens": 3000,
            "reasoning_effort": "low",
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
        },
    )
    assert st == 200, body
    calls = body["choices"][0]["message"].get("tool_calls") or []
    assert calls and calls[0]["function"]["name"] == "get_weather", body


checks = [
    ("GET /v1/models", models),
    ("chat basic", chat_basic),
    ("pass-through alias nutrient-m", chat_alias_nutrient),
    ("chat stream", chat_stream),
    ("embeddings 512d", embeddings),
    ("unknown model -> 404", bad_model),
    ("function calling", tools),
]

results = [check(n, f) for n, f in checks]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
