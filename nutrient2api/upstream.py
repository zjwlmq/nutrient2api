"""HTTP client for the Nutrient AI Gateway.

Upstream quirks that shape this module:

- ``max_tokens`` is rewritten to ``max_completion_tokens`` before it reaches
  the model, so callers sending either name work.
- Reasoning models reject ``function tools`` together with a non-none
  ``reasoning_effort`` on ``/chat/completions``. We retry through
  ``/responses`` when that specific error appears.
- Errors come back verbatim from OpenAI, including the real model name.
"""

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, Optional

DEFAULT_BASE = "https://api.nutrient.io/ai/v1"
RETRYABLE = {429, 500, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"upstream {status}: {payload}")
        self.status = status
        self.payload = payload


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return {"error": {"message": raw.decode(errors="replace"), "type": "upstream_error"}}


def _request(
    url: str,
    api_key: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 300,
    retries: int = 3,
) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {api_key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = _decode(resp.read())
                if not isinstance(result, dict) or result.get("error"):
                    raise UpstreamError(502, result if isinstance(result, dict) else {
                        "error": {"message": "unexpected upstream response"}
                    })
                return result
        except urllib.error.HTTPError as exc:
            with exc:
                payload_err = _decode(exc.read())
            if exc.code in RETRYABLE and attempt < retries:
                last = UpstreamError(exc.code, payload_err)
                try:
                    delay = float(exc.headers.get("Retry-After", 2 ** attempt))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(max(0, min(delay, 30)))
                continue
            raise UpstreamError(exc.code, payload_err)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last = exc
            # A timed-out generation may already have executed upstream. Only
            # retry transport failures on GET, to avoid replaying billed POSTs.
            if body is None and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise UpstreamError(504, {"error": {"message": f"upstream unreachable: {exc}"}})
    raise UpstreamError(502, {"error": {"message": f"retries exhausted: {last}"}})


def _error_code(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        if isinstance(err, dict):
            return str(err.get("code") or "")
    return ""


def _error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        if isinstance(err, dict):
            return str(err.get("message") or "")
    return ""


def chat(
    api_key: str,
    model: str,
    body: Dict[str, Any],
    base_url: str = DEFAULT_BASE,
    policy: Optional[Any] = None,
    retries: int = 3,
) -> Dict[str, Any]:
    """POST /chat/completions, falling back to /responses for tool+reasoning."""
    payload = dict(body)
    payload["model"] = model
    if policy is not None and policy.active:
        payload["messages"] = policy.apply(payload.get("messages") or [])
    if "max_tokens" in payload:
        payload.setdefault("max_completion_tokens", payload.pop("max_tokens"))

    try:
        result = _request(f"{base_url}/chat/completions", api_key, payload, retries=retries)
    except UpstreamError as exc:
        if not _is_tool_reasoning_conflict(exc.payload):
            raise
        result = _via_responses(api_key, model, payload, base_url, retries=retries)
    return _scrub_result(result, policy)


def _is_tool_reasoning_conflict(payload: Any) -> bool:
    msg = _error_message(payload)
    return "Function tools with reasoning_effort are not supported" in msg


def _scrub_result(result: Dict[str, Any], policy: Optional[Any]) -> Dict[str, Any]:
    """Strip provider self-identification from a completed chat payload."""
    if policy is None or not getattr(policy, "suppress_claim", False):
        return result
    for choice in result.get("choices") or []:
        message = choice.get("message") or {}
        if isinstance(message.get("content"), str):
            message["content"] = policy.scrub(message["content"])
    return result


def _via_responses(
    api_key: str, model: str, payload: Dict[str, Any], base_url: str, retries: int = 3
) -> Dict[str, Any]:
    """Translate a Chat Completions request into a Responses request."""
    if payload.get("n", 1) != 1:
        raise UpstreamError(400, {"error": {"message": "tool/reasoning fallback only supports n=1"}})
    messages = payload.get("messages") or []
    instructions = "\n\n".join(
        _text_content(m.get("content")) for m in messages if m.get("role") in ("system", "developer")
    )
    turns = [m for m in messages if m.get("role") not in ("system", "developer")]

    resp_req: Dict[str, Any] = {
        "model": model,
        "input": _responses_input(turns),
        "max_output_tokens": payload.get("max_completion_tokens") or 4096,
    }
    if instructions:
        resp_req["instructions"] = instructions
    for key in ("temperature", "top_p", "store", "metadata", "user", "parallel_tool_calls", "service_tier"):
        if key in payload:
            resp_req[key] = payload[key]
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        fmt = dict(response_format)
        if fmt.get("type") == "json_schema":
            fmt = {"type": "json_schema", **fmt.get("json_schema", {})}
        resp_req["text"] = {"format": fmt}

    tools = _to_responses_tools(payload.get("tools"))
    if tools:
        resp_req["tools"] = tools
        choice = payload.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function":
            choice = {"type": "function", "name": (choice.get("function") or {}).get("name")}
        if choice in ("auto", "none", "required") or isinstance(choice, dict):
            resp_req["tool_choice"] = choice
    effort = payload.get("reasoning_effort")
    if effort:
        resp_req["reasoning"] = {"effort": effort}

    result = _request(f"{base_url}/responses", api_key, resp_req, retries=retries)
    return _responses_to_chat(result, model)


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def _responses_input(messages: Any) -> list:
    items = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                raise UpstreamError(400, {"error": {"message": "tool result requires tool_call_id"}})
            items.append({
                "type": "function_call_output", "call_id": call_id,
                "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            })
            continue
        if role not in ("user", "assistant"):
            raise UpstreamError(400, {"error": {"message": "unsupported role in Responses fallback"}})
        if content is not None:
            blocks = _content_blocks(content, assistant=role == "assistant")
            # EasyInputMessage accepts assistant text as a string; output_text
            # objects instead belong to full output messages with IDs/status.
            item = {"role": role, "content": "".join(b["text"] for b in blocks) if role == "assistant" else blocks}
            if role == "assistant" and "phase" in message:
                item["phase"] = message["phase"]
            items.append(item)
        for call in message.get("tool_calls") or []:
            if call.get("type") != "function":
                raise UpstreamError(400, {"error": {"message": "fallback only supports function tools"}})
            fn = call.get("function") or {}
            items.append({
                "type": "function_call", "call_id": call.get("id"),
                "name": fn.get("name"), "arguments": fn.get("arguments", "{}"),
            })
    return items


def _content_blocks(content: Any, assistant: bool = False) -> Any:
    text_type = "output_text" if assistant else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if isinstance(content, list):
        blocks = []
        for part in content:
            if not isinstance(part, dict):
                raise UpstreamError(400, {"error": {"message": "invalid content block"}})
            if part.get("type") == "text":
                blocks.append({"type": text_type, "text": part.get("text", "")})
            elif part.get("type") == "image_url" and not assistant:
                image = part.get("image_url") or {}
                block = {"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image}
                if isinstance(image, dict) and "detail" in image:
                    block["detail"] = image["detail"]
                blocks.append(block)
            elif part.get("type") == "file" and not assistant:
                file = part.get("file") or {}
                blocks.append({"type": "input_file", **{k: file[k] for k in ("file_id", "file_data", "filename") if k in file}})
            else:
                raise UpstreamError(400, {"error": {"message": "unsupported content type in Responses fallback"}})
        return blocks or [{"type": text_type, "text": ""}]
    return [{"type": text_type, "text": "" if content is None else str(content)}]


def _to_responses_tools(tools: Any) -> list:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise UpstreamError(400, {"error": {"message": "fallback only supports function tools"}})
        fn = tool.get("function") or {}
        entry = {"type": "function", "name": fn.get("name"), "parameters": fn.get("parameters") or {}}
        if fn.get("description"):
            entry["description"] = fn["description"]
        if "strict" in fn:
            entry["strict"] = fn["strict"]
        out.append(entry)
    return out


def _responses_to_chat(result: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Normalise a Responses payload into a Chat Completions payload."""
    if result.get("error") or result.get("status") == "failed":
        raise UpstreamError(502, {"error": result.get("error") or {"message": "upstream response failed"}})
    text_parts = []
    refusals = []
    tool_calls = []
    for item in result.get("output") or []:
        if item.get("type") == "message":
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "refusal":
                    refusals.append(block.get("refusal", ""))
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )

    usage = result.get("usage") or {}
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if refusals:
        message["refusal"] = "".join(refusals)
    finish_reason = "tool_calls" if tool_calls else "stop"
    incomplete_reason = (result.get("incomplete_details") or {}).get("reason")
    if incomplete_reason == "max_output_tokens":
        finish_reason = "length"
    elif incomplete_reason == "content_filter":
        finish_reason = "content_filter"
    chat_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    for old, new in (("input_tokens_details", "prompt_tokens_details"), ("output_tokens_details", "completion_tokens_details")):
        if old in usage:
            chat_usage[new] = usage[old]

    return {
        "id": result.get("id") or "chatcmpl-mapped",
        "object": "chat.completion",
        "created": result.get("created_at") or 0,
        "model": result.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": chat_usage,
    }


def chat_stream(
    api_key: str,
    model: str,
    body: Dict[str, Any],
    base_url: str = DEFAULT_BASE,
    policy: Optional[Any] = None,
    retries: int = 3,
) -> Iterator[str]:
    """Yield complete SSE events; buffer only the tool/reasoning fallback."""
    payload = dict(body)
    payload["model"] = model
    payload["stream"] = True
    if policy is not None and policy.active:
        payload["messages"] = policy.apply(payload.get("messages") or [])
    if "max_tokens" in payload:
        payload.setdefault("max_completion_tokens", payload.pop("max_tokens"))

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as exc:
        with exc:
            error = _decode(exc.read())
        if not _is_tool_reasoning_conflict(error):
            raise UpstreamError(exc.code, error)
        result = _scrub_result(_via_responses(api_key, model, payload, base_url, retries=retries), policy)
        yield from _completed_stream(result, payload)
        return
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise UpstreamError(504, {"error": {"message": "upstream connection failed"}}) from exc

    scrubber = _StreamScrubber(policy) if _wants_scrub(policy) else None
    with resp:
        if resp.headers.get_content_type() != "text/event-stream":
            raise UpstreamError(502, {"error": {"message": "upstream did not return an event stream"}})
        try:
            for event in _sse_events(resp):
                data = event_data(event)
                if data and data != "[DONE]":
                    try:
                        value = json.loads(data)
                    except ValueError:
                        value = None
                    if isinstance(value, dict) and value.get("error"):
                        raise UpstreamError(502, value)
                if scrubber is not None:
                    yield from scrubber.process(event, model)
                else:
                    yield event
                if event_data(event) == "[DONE]":
                    return
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamError(504, {"error": {"message": "upstream stream interrupted"}}) from exc


def encode_sse(payload: Any) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def event_data(event: str) -> str:
    return "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))


def _sse_events(resp: Any) -> Iterator[str]:
    lines = []
    size = 0
    limit = 1024 * 1024
    while True:
        raw = resp.readline(limit + 1)
        if not raw:
            raise UpstreamError(502, {"error": {"message": "upstream stream ended before [DONE]"}})
        size += len(raw)
        if size > limit:
            raise UpstreamError(502, {"error": {"message": "upstream SSE event exceeds size limit"}})
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            lines.append(line)
            # Some gateways keep the socket open even after the terminator.
            if len(lines) == 1 and event_data(line) == "[DONE]":
                yield "data: [DONE]\n\n"
                return
        else:
            if lines:
                yield "\n".join(lines) + "\n\n"
            lines, size = [], 0


def _completed_stream(result: Dict[str, Any], request: Dict[str, Any]) -> Iterator[str]:
    """Encode a buffered Responses fallback in the Chat Completions SSE format."""
    common = {k: result[k] for k in ("id", "created", "model")}
    common["object"] = "chat.completion.chunk"
    for choice in result["choices"]:
        delta = dict(choice["message"])
        if "tool_calls" in delta:
            delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(delta["tool_calls"])]
        yield encode_sse({**common, "choices": [{"index": choice["index"], "delta": delta, "finish_reason": None}]})
        yield encode_sse({**common, "choices": [{"index": choice["index"], "delta": {}, "finish_reason": choice["finish_reason"]}]})
    if (request.get("stream_options") or {}).get("include_usage"):
        yield encode_sse({**common, "choices": [], "usage": result["usage"]})
    yield "data: [DONE]\n\n"


def _wants_scrub(policy: Optional[Any]) -> bool:
    return policy is not None and getattr(policy, "suppress_claim", False)


# Any of these can begin an identity claim, so text is never cut right after one.
_CLAIM_TRIGGERS = re.compile(
    r"\b(?:created|made|built|developed|trained|ChatGPT|OpenAI)\b", re.I
)


class _StreamScrubber:
    """Delays stream output just enough to match claims spanning chunks.

    A claim like "created by OpenAI" arrives split across deltas, so scrubbing
    each delta in isolation misses it. Text is held back until no claim can
    still be forming at the cut point.
    """

    def __init__(self, policy: Any, hold: int = 48, lookback: int = 96) -> None:
        self.policy = policy
        self.hold = hold
        self.lookback = lookback
        self.buf = ""
        self._choices: Dict[int, Any] = {}
        self._meta: Dict[str, Any] = {}

    def feed(self, text: str) -> str:
        self.buf += text
        if len(self.buf) <= self.hold + self.lookback:
            return ""
        cut = len(self.buf) - self.hold
        window_start = max(0, cut - self.lookback)
        hits = list(_CLAIM_TRIGGERS.finditer(self.buf, window_start, cut))
        if hits:
            cut = hits[-1].start()
        if cut <= 0:
            return ""
        out = self.policy.scrub(self.buf[:cut])
        self.buf = self.buf[cut:]
        return out

    def flush(self) -> str:
        out = self.policy.scrub(self.buf) if self.buf else ""
        self.buf = ""
        return out

    def process(self, line: str, model: str) -> Iterator[str]:
        body = event_data(line)
        if not body:
            yield line
            return
        if body == "[DONE]":
            for index, buffer in self._choices.items():
                tail = buffer.flush()
                if tail:
                    yield encode_sse({**self._meta, "choices": [{"index": index, "delta": {"content": tail}, "finish_reason": None}]})
            yield line
            return
        try:
            chunk = json.loads(body)
        except Exception:
            yield line
            return

        if not isinstance(chunk, dict) or not chunk.get("choices"):
            yield line
            return
        self._meta = {k: chunk[k] for k in ("id", "object", "created", "model") if k in chunk}
        self._meta.setdefault("model", model)
        kept = []
        for choice in chunk.get("choices") or []:
            index = choice.get("index", 0)
            if index not in self._choices:
                self._choices[index] = _StreamScrubber(self.policy, self.hold, self.lookback)
            buffer = self._choices[index]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            safe = ""
            if isinstance(piece, str) and piece:
                safe = buffer.feed(piece)
                delta.pop("content", None)
            if choice.get("finish_reason") is not None:
                safe += buffer.flush()
            if safe:
                delta["content"] = safe
            choice["delta"] = delta
            if not delta and choice.get("finish_reason") is None:
                continue
            kept.append(choice)
        if not kept:
            return
        chunk["choices"] = kept
        yield encode_sse(chunk)


def _synthetic_chunk(model: str, text: str) -> str:
    return "data: " + json.dumps(
        {
            "id": "chatcmpl-scrub-tail",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        },
        ensure_ascii=False,
    ) + "\n\n"


def embeddings(
    api_key: str, model: str, body: Dict[str, Any], base_url: str = DEFAULT_BASE, retries: int = 3
) -> Dict[str, Any]:
    payload = dict(body)
    payload["model"] = model
    result = _request(f"{base_url}/embeddings", api_key, payload, retries=retries)
    if isinstance(result, dict):
        result["model"] = body.get("model") or model
    return result


def upstream_models(api_key: str, base_url: str = DEFAULT_BASE) -> Any:
    return _request(f"{base_url}/models", api_key)


Transport = Callable[..., Dict[str, Any]]
