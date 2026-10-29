"""Offline tests for prompt policy. No network, no API key needed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nutrient2api.prompt import PromptPolicy, _flag  # noqa: E402


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
        return True
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False


def passthrough():
    msgs = [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]
    out = PromptPolicy().apply(msgs)
    assert out == msgs, out


def drop_caller():
    msgs = [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]
    out = PromptPolicy(drop_caller_system=True).apply(msgs)
    assert out == [{"role": "user", "content": "hi"}], out


def append_ours():
    msgs = [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]
    out = PromptPolicy(system_prompt="ours").apply(msgs)
    assert [m["role"] for m in out] == ["system", "system", "user"], out
    assert out[1]["content"] == "ours", out
    # ours must sit after the caller's so it wins recency
    assert out[-1]["role"] == "user", out


def replace_mode():
    msgs = [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]
    out = PromptPolicy(system_prompt="ours", drop_caller_system=True).apply(msgs)
    assert [m["role"] for m in out] == ["system", "user"], out
    assert out[0]["content"] == "ours", out


def authority_tail():
    out = PromptPolicy(system_prompt="ours", assert_authority=True).apply(
        [{"role": "user", "content": "hi"}]
    )
    assert "take precedence" in out[0]["content"], out
    assert out[0]["content"].startswith("ours"), out


def scrub_identity():
    p = PromptPolicy(suppress_claim=True)
    cases = [
        ("I'm an AI assistant created by OpenAI.", "I am an AI assistant"),
        ("I am ChatGPT by OpenAI; my exact model name isn't available.", "I am an AI assistant"),
        ("I'm ChatGPT and I like it.", "I am an AI assistant"),
    ]
    for raw, want in cases:
        got = p.scrub(raw)
        assert want in got, (raw, got)
        assert "OpenAI" not in got, got
        assert "ChatGPT" not in got, got


def scrub_off_by_default():
    assert PromptPolicy().scrub("I'm ChatGPT by OpenAI.") == "I'm ChatGPT by OpenAI."


def scrub_none_safe():
    assert PromptPolicy(suppress_claim=True).scrub(None) is None


def active_flag():
    assert not PromptPolicy().active
    assert PromptPolicy(suppress_claim=True).active


def env_flags():
    os.environ["NUTRIENT2API_DROP_CALLER_SYSTEM"] = "1"
    os.environ["NUTRIENT2API_SUPPRESS_CLAIM"] = "true"
    try:
        p = PromptPolicy.from_env()
        assert p.drop_caller_system and p.suppress_claim, "env flags not read"
    finally:
        del os.environ["NUTRIENT2API_DROP_CALLER_SYSTEM"]
        del os.environ["NUTRIENT2API_SUPPRESS_CLAIM"]
    assert not _flag("NUTRIENT2API_SUPPRESS_CLAIM")


checks = [
    ("passthrough when inactive", passthrough),
    ("drop caller system", drop_caller),
    ("append ours after caller", append_ours),
    ("replace mode", replace_mode),
    ("authority tail", authority_tail),
    ("scrub identity claims", scrub_identity),
    ("scrub off by default", scrub_off_by_default),
    ("scrub handles None", scrub_none_safe),
    ("active flag", active_flag),
    ("env flags", env_flags),
]

results = [check(n, f) for n, f in checks]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)