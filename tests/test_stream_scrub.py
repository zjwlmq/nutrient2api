"""Offline tests for the streaming claim scrubber. No network."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nutrient2api.prompt import PromptPolicy  # noqa: E402
from nutrient2api.upstream import _StreamScrubber  # noqa: E402


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
        return True
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False


def delta(piece):
    return "data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
    ) + "\n\n"


def collect(lines):
    """Run SSE lines through the scrubber and reassemble the visible text."""
    s = _StreamScrubber(PromptPolicy(suppress_claim=True))
    out_text = ""
    for line in lines:
        for emitted in s.process(line, "gpt-6-luna"):
            if not emitted.startswith("data: "):
                continue
            body = emitted[6:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                chunk = json.loads(body)
            except Exception:
                continue
            for c in chunk.get("choices") or []:
                out_text += (c.get("delta") or {}).get("content") or ""
    return out_text


def split_claim_across_chunks():
    parts = ["I'm an AI assistant ", "created", " by ", "OpenAI", "."]
    text = collect([delta(p) for p in parts] + ["data: [DONE]\n\n"])
    assert "OpenAI" not in text, text
    assert "created by" not in text, text


def clean_text_survives():
    parts = ["Hello", ", ", "world", "."]
    text = collect([delta(p) for p in parts] + ["data: [DONE]\n\n"])
    assert text == "Hello, world.", repr(text)


def long_text_flushes():
    parts = ["lorem ipsum dolor sit amet "] * 12
    text = collect([delta(p) for p in parts] + ["data: [DONE]\n\n"])
    assert len(text) > 200, len(text)
    assert "lorem ipsum" in text, text[:80]


def tail_is_emitted():
    text = collect([delta("x" * 5)] + ["data: [DONE]\n\n"])
    assert text == "x" * 5, repr(text)


def converse_identity_kept_when_off():
    s = _StreamScrubber(PromptPolicy())  # scrub disabled
    text = ""
    for line in [delta("I'm ChatGPT by OpenAI."), "data: [DONE]\n\n"]:
        for emitted in s.process(line, "gpt-6-luna"):
            if emitted.startswith("data: ") and "[DONE]" not in emitted:
                chunk = json.loads(emitted[6:])
                for c in chunk.get("choices") or []:
                    text += (c.get("delta") or {}).get("content") or ""
    assert "OpenAI" in text, text


checks = [
    ("claim split across chunks", split_claim_across_chunks),
    ("clean text unchanged", clean_text_survives),
    ("long text flushes", long_text_flushes),
    ("short tail emitted", tail_is_emitted),
    ("no scrub when disabled", converse_identity_kept_when_off),
]

results = [check(n, f) for n, f in checks]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)