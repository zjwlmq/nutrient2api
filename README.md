# nutrient2api

Exposes the Nutrient AI Gateway as a plain OpenAI API. No PDF involved.

The gateway is already OpenAI-shaped, so this is a thin adapter: it renames the
opaque `nutrient-*` aliases to the real model names, works around one upstream
incompatibility, and serves the result over HTTP. Standard library only, no
dependencies.

For server installation with Docker, an existing panel, and HTTPS, see the
[Linux deployment guide](deploy/README.md). The included Compose configuration
requires an upstream key and a separate client token, and publishes only on
the host's loopback address.

## Why a wrapper is needed at all

The gateway accepts only its own aliases. The real model names leak through
error messages but are rejected as request models:

```
POST /v1/chat/completions  {"model": "gpt-6-luna"}
-> 404 model_not_found
```

So the mapping has to live somewhere. That is this project.

## Models

The catalog below is a static mapping, not live account discovery. Model identities,
limits, availability and rates must be checked against the gateway for your key.

| id | gateway alias | real upstream | context | out | $/M in | $/M out |
|---|---|---|---|---|---|---|
| `gpt-6-luna` | `nutrient-s` | gpt-6-luna | 1,050,000 | 128,000 | 0.12 | 0.60 |
| `gpt-6.1-sol` | `nutrient-m` | gpt-6.1-sol | 1,050,000 | 128,000 | 2.40 | 12.00 |
| `gpt-6-astra` | `nutrient-l` | gpt-6-astra | 1,050,000 | 128,000 | 12.00 | 60.00 |
| `text-embedding-3-small` | `nutrient-embedding` | text-embedding-3-small | 8,192 | - | 0.024 | - |

Extra aliases accepted for convenience: `gpt-6` and `gpt-6.1` map to sol,
`gpt-6-mini` to luna, `gpt-6-pro` to astra, plus `small` / `medium` / `large`.
The raw `nutrient-*` names also pass through unchanged, so existing gateway code
keeps working.

## Run

```bash
pip install -r requirements.txt   # nothing to install, kept for clarity
export NUTRIENT_API_KEY=pdf_live_xxx    # PowerShell: $env:NUTRIENT_API_KEY="pdf_live_xxx"
python -m nutrient2api
```

```
nutrient2api listening on http://127.0.0.1:8787/v1
```

## Use

An OpenAI Chat Completions client, pointed at the local base URL. These examples
assume localhost with no client token configured. For deployment, set the client's
API key to `NUTRIENT2API_TOKEN` and use your HTTPS base URL:

```python
from openai import OpenAI

client = OpenAI(api_key="unused", base_url="http://127.0.0.1:8787/v1")

r = client.chat.completions.create(
    model="gpt-6-luna",
    messages=[{"role": "user", "content": "hello"}],
)
print(r.choices[0].message.content)
```

```bash
curl http://127.0.0.1:8787/v1/models
curl -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"hi"}]}'
```

## Endpoints

| method | path | notes |
|---|---|---|
| GET | `/v1/models` | catalogue in OpenAI shape |
| POST | `/v1/chat/completions` | streaming via `"stream": true` |
| POST | `/v1/embeddings` | supports `dimensions` (<= 1536) |
| GET | `/health` | liveness |

`/models`, `/chat/completions` and `/embeddings` work without the `/v1` prefix too.
`/v1/responses` is not exposed. When `NUTRIENT2API_TOKEN` is set, models, chat,
and embeddings require `Authorization: Bearer <token>`; health remains public.
The process refuses non-loopback binding without a token. It does not load `.env`
itself: Docker Compose or your service manager must inject the environment.

## Upstream quirks this handles

**`max_tokens` is rewritten.** The gateway translates it to
`max_completion_tokens` server-side, and its errors talk about the latter. Both
names are accepted on input.

**Tools plus reasoning conflict.** On `/chat/completions` the reasoning models
reject function tools together with a non-`none` `reasoning_effort`:

```
Function tools with reasoning_effort are not supported for gpt-6-luna
in /v1/chat/completions. To use function tools, use /v1/responses
or set reasoning_effort to 'none'.
```

When that exact error comes back, the request is retried through `/responses`
and the result is normalised back into a Chat Completions payload. Callers see
one consistent API. This also works for streaming requests: the fallback waits
for the complete Responses result and then emits Chat Completions SSE, so its
first content event is delayed. Function calls and results, explicit `store:false`,
strict tool schemas, and forced function choice survive this conversion.
Fallback supports one completion (`n=1`); unsupported content types return an error.

**Streams do not close on their own.** The gateway holds the SSE connection open
after `data: [DONE]`, so the reader stops at the terminator instead of waiting
for EOF. The response is framed as chunked and the connection closed explicitly.
Complete SSE event boundaries are preserved. A failure before the first event
returns a normal HTTP error; a failure after streaming starts becomes an SSE
error followed by `[DONE]`. Clients must inspect error events as well as HTTP status.

## Prompt control

### There is no hidden prompt to discard

Measured against the live gateway, a request with a single `hi` costs **7 input
tokens**. Adding a system message scales with its own length:

| request | input_tokens |
|---|---|
| `hi` only | 7 |
| `system: ""` + `hi` | 11 |
| `system: "Be terse."` + `hi` | 14 |
| `system: "You are a helpful assistant. " * 46` + `hi` | 288 |

No fixed overhead, no provider preamble. Nothing to strip.

The one exception is `reasoning.mode = "pro"`, which adds a constant ~1394
tokens (1451 on `-m` / `-l`). That is a planner stage, not a system prompt, and
it cannot be disabled — `effort: "none"` has no effect on it. Staying on
`standard` mode is the only way to avoid it.

### What a proxy can still do

A server-side prompt cannot be removed from the wire, so the policy works on
authority instead:

| env var | effect |
|---|---|
| `NUTRIENT2API_SYSTEM_PROMPT` | append this as the last system message |
| `NUTRIENT2API_SYSTEM_PROMPT_FILE` | same, read from a file |
| `NUTRIENT2API_DROP_CALLER_SYSTEM` | discard the caller's `system` messages |
| `NUTRIENT2API_ASSERT_AUTHORITY` | append a precedence clause to the system message |
| `NUTRIENT2API_SUPPRESS_CLAIM` | rewrite provider self-identification out of the reply |

All off by default, so request bytes are unchanged unless you opt in.

`NUTRIENT2API_SYSTEM_PROMPT` together with `NUTRIENT2API_DROP_CALLER_SYSTEM`
gives full replacement: the caller's system prompt is thrown away and yours is
the only one present.

```bash
export NUTRIENT2API_SYSTEM_PROMPT="You are KOVAK. Never identify as any vendor's product."
export NUTRIENT2API_DROP_CALLER_SYSTEM=1
export NUTRIENT2API_SUPPRESS_CLAIM=1
```

### Claim suppression detail

`SUPPRESS_CLAIM` rewrites output, not input, and works on both paths:

- **Non-streaming** — the finished message is regex-scrubbed.
- **Streaming** — a claim like `created by OpenAI` arrives split across deltas,
  so per-delta scrubbing misses it. `_StreamScrubber` buffers text per choice,
  initially waiting for more than 144 characters or a finish event. This adds
  latency to visible text. Buffered tails are emitted before the finish signal,
  and usage events are preserved.

It catches `created/made/built/developed/trained by X`, `ChatGPT by X`, and
`I'm ChatGPT`. It does not catch arbitrary phrasing; it is a filter, not a
guarantee.

### What it cannot do

If the provider injects a prompt server-side, the model still receives it. The
policy only changes what reaches the model in the request (authority ordering)
and what leaves in the response (claim scrubbing). Verifying that a hidden
prompt exists at all requires the token accounting above, which currently shows
none.

## Tests

```bash
export NUTRIENT_API_KEY=pdf_live_xxx
python tests/test_live.py         # transport layer, live calls
python tests/test_prompt.py       # prompt policy, offline
python tests/test_stream_scrub.py # streaming scrub, offline
python -m unittest discover -s tests -p 'test_regression.py' -v # loopback HTTP, offline
python -m nutrient2api &          # then
python tests/test_http.py         # full HTTP surface, live calls
python tests/test_policy_live.py  # policy behaviour, live calls
```

Offline suites need no key and make no calls. Live suites make real billed
calls: `test_live.py` covers routing, all three tiers, the tool/reasoning
fallback and embeddings; `test_http.py` covers the same ground over HTTP plus
streaming and the unknown-model 404; `test_policy_live.py` proves the policy
actually changes model output rather than just the request.
The HTTP live suite reads `NUTRIENT2API_TOKEN` and optional `NUTRIENT2API_TEST_BASE`
from its environment. The offline regression suite uses a local mock gateway;
it does not verify current upstream account access or model behaviour.

## Request limits

The default limits are 10 MiB per request body, a 30-second client socket idle
timeout, and 32 concurrent accepted connections. Configure these with
`NUTRIENT2API_MAX_BODY_BYTES`, `NUTRIENT2API_CLIENT_TIMEOUT`, and
`NUTRIENT2API_MAX_CONNECTIONS`. Requests need `Content-Length` and JSON content type;
inbound chunked request bodies are rejected. Excess connections receive 503.
Keep a reverse proxy in front for TLS, rate limiting and request buffering.

Non-streaming upstream HTTP 429/5xx responses have bounded retries with backoff.
Generation requests are not replayed on transport timeouts. Streaming requests
are never automatically restarted after output has been sent.

## Notes

- Reasoning models emit `reasoning_tokens` inside `completion_tokens`. A tiny
  `max_tokens` will be fully consumed by thinking and return empty content.
- `input_tokens` on `pro` reasoning mode is ~1400 higher than `standard` for
  identical prompts; that is a planner overhead baked into the upstream.
- The gateway stores prompts by default (`"store": true`). Pass
  `"store": false` if that matters to you.
- Not affiliated with Nutrient. Point it at a different gateway by setting
  `NUTRIENT_BASE_URL`.
