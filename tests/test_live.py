import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nutrient2api import catalog, server, upstream  # noqa: E402

client = server.Config()
if not client.api_key:
    print("SKIP: set NUTRIENT_API_KEY to run live tests")
    sys.exit(0)


def direct(model_id, body):
    m = catalog.resolve(model_id)
    assert m is not None, f"unresolved {model_id}"
    return upstream.chat(client.api_key, m.upstream, body, client.base_url)


def test(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
        return True
    except Exception as exc:
        print(f"FAIL  {name}: {exc}")
        return False


def routing_table():
    assert catalog.resolve("nutrient-s").upstream == "nutrient-s"
    assert catalog.resolve("nutrient-s").real == "gpt-6-luna"
    assert catalog.resolve("gpt-6-luna").upstream == "nutrient-s"
    assert catalog.resolve("gpt-6.1-sol").upstream == "nutrient-m"
    assert catalog.resolve("gpt-6-astra").upstream == "nutrient-l"
    assert catalog.resolve("gpt-6").upstream == "nutrient-m"
    assert catalog.resolve("nonexistent-model") is None


def basic():
    r = direct("gpt-6-luna", {"messages": [{"role": "user", "content": "reply: ok"}], "max_tokens": 2000})
    assert r["choices"][0]["message"]["content"], r
    assert r["usage"]["total_tokens"] > 0


def all_tiers():
    for mid in ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"):
        r = direct(mid, {"messages": [{"role": "user", "content": "one word: alpha"}], "max_tokens": 2000})
        assert r["choices"][0]["message"]["content"], (mid, r)


def tools_via_responses():
    r = direct(
        "gpt-6-luna",
        {
            "messages": [{"role": "user", "content": "Weather in Paris? Use the tool."}],
            "max_tokens": 3000,
            "reasoning_effort": "low",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
        },
    )
    calls = r["choices"][0]["message"].get("tool_calls") or []
    assert calls and calls[0]["function"]["name"] == "get_weather", r


def tools_none_effort():
    r = direct(
        "gpt-6-luna",
        {
            "messages": [{"role": "user", "content": "Weather in Rome? Use the tool."}],
            "max_tokens": 3000,
            "reasoning_effort": "none",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
        },
    )
    calls = r["choices"][0]["message"].get("tool_calls") or []
    assert calls, r


def embeddings():
    r = upstream.embeddings(
        client.api_key, "nutrient-embedding", {"input": "probe", "dimensions": 256}, client.base_url
    )
    assert len(r["data"][0]["embedding"]) == 256


def stream_ok():
    lines = list(
        upstream.chat_stream(
            client.api_key,
            "nutrient-s",
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2000},
            client.base_url,
        )
    )
    assert any("[DONE]" in l for l in lines), lines[-3:]


checks = [
    ("routing table", routing_table),
    ("basic chat", basic),
    ("all tiers", all_tiers),
    ("tools + reasoning -> responses", tools_via_responses),
    ("tools + effort none -> chat", tools_none_effort),
    ("embeddings dimensions", embeddings),
    ("stream sse", stream_ok),
]

results = [test(n, f) for n, f in checks]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)