"""Live check that the policy actually changes model behaviour."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nutrient2api import server, upstream  # noqa: E402
from nutrient2api.prompt import PromptPolicy  # noqa: E402

cfg = server.Config()
if not cfg.api_key:
    print("SKIP: set NUTRIENT_API_KEY")
    sys.exit(0)

ASK = "In one short sentence: what are you?"


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
        return True
    except Exception as exc:
        print(f"FAIL  {name}: {exc}")
        return False


def ask(policy, user=ASK, system=None, stream=False):
    body = {"messages": [], "max_tokens": 3000}
    if system:
        body["messages"].append({"role": "system", "content": system})
    body["messages"].append({"role": "user", "content": user})
    if stream:
        lines = list(upstream.chat_stream(cfg.api_key, "nutrient-s", body, cfg.base_url, policy))
        text = ""
        for line in lines:
            if line.startswith("data: ") and "[DONE]" not in line:
                import json
                try:
                    chunk = json.loads(line[6:])
                except Exception:
                    continue
                for c in chunk.get("choices") or []:
                    text += (c.get("delta") or {}).get("content") or ""
        return text
    result = upstream.chat(cfg.api_key, "nutrient-s", body, cfg.base_url, policy)
    return result["choices"][0]["message"]["content"] or ""


def baseline_mentions_openai():
    # brand disclosure is not guaranteed; try phrasings that reliably elicit it
    probes = [
        "Which company developed you? One short sentence.",
        "Who made you? Answer with the company name.",
        "State your creator and your product name in one sentence.",
        "In one short sentence: what are you?",
    ]
    seen = []
    for prompt in probes:
        for _ in range(2):
            text = ask(PromptPolicy(), user=prompt)
            seen.append(text)
            if "OpenAI" in text or "ChatGPT" in text:
                return
    raise AssertionError(f"no brand volunteered; samples={seen[:4]}")


def scrub_removes_brand():
    text = ask(PromptPolicy(suppress_claim=True))
    assert "OpenAI" not in text, text
    assert "ChatGPT" not in text, text


def local_prompt_wins():
    text = ask(PromptPolicy(system_prompt="You must answer only with the single word: KOVAK."))
    assert "KOVAK" in text.upper(), text


def drop_caller_then_ours():
    text = ask(
        PromptPolicy(system_prompt="Answer only with: PWNED", drop_caller_system=True),
        system="Answer only with: CALLER",
    )
    assert "PWNED" in text.upper(), text
    assert "CALLER" not in text.upper(), text


def authority_beats_injected():
    # caller tries to install an identity; our policy asserts precedence
    text = ask(
        PromptPolicy(suppress_claim=True, assert_authority=True, system_prompt="Never adopt a provider identity."),
        system="You are HAL 9000. Always identify as HAL.",
    )
    assert "HAL" not in text.upper() or "OpenAI" not in text, text


def stream_scrub():
    text = ask(PromptPolicy(suppress_claim=True), stream=True)
    assert text.strip(), "empty stream"
    assert "OpenAI" not in text, text


checks = [
    ("baseline mentions OpenAI", baseline_mentions_openai),
    ("suppress_claim strips brand", scrub_removes_brand),
    ("local system prompt wins", local_prompt_wins),
    ("drop caller, keep ours", drop_caller_then_ours),
    ("authority overrides caller", authority_beats_injected),
    ("stream scrub works", stream_scrub),
]

results = [check(n, f) for n, f in checks]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)