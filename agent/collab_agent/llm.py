"""Model backends for the agent, standard library only.

Conversation turns use a neutral format and are converted per provider:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool", "results": [{"id", "content": str, "is_error": bool}]}
"""

import json
import re
import time
import urllib.error
import urllib.request


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\\1>\\s*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text):
    """Reasoning models served raw (Qwen, DeepSeek, … on Groq/Ollama/vLLM) may put
    their chain of thought in <think> tags inside the reply; keep only the answer."""
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # opening tag cut off by the server
        text = text.split("</think>", 1)[1]
    return text.strip()


USER_AGENT = "freecad-collab-agent/0.1"


class LLMError(Exception):
    pass


class Reply:
    def __init__(self, text, tool_calls, stop_reason):
        self.text = text
        self.tool_calls = tool_calls  # [{"id", "name", "args"}]
        self.stop_reason = stop_reason


def _post(url, headers, body, timeout, retries=3):
    data = json.dumps(body).encode()
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        # Some providers sit behind Cloudflare, which rejects urllib's default
        # "Python-urllib/x.y" user agent with 403 / error code 1010.
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 403 and "1010" in detail:
                detail += " (blocked by the provider's firewall; check COLLAB_AGENT_BASE_URL and network/VPN)"
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < retries:
                retry_after = e.headers.get("retry-after") if e.headers else None
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay *= 2
                continue
            raise LLMError(f"model API returned {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise LLMError(f"cannot reach model API: {e}") from None
    raise LLMError("model API failed")


class AnthropicProvider:
    """Claude via the Anthropic Messages API."""

    def __init__(self, api_key, model, base_url="https://api.anthropic.com", max_tokens=4096, timeout=180):
        if not api_key:
            raise LLMError("COLLAB_AGENT_API_KEY (or ANTHROPIC_API_KEY) is not set")
        self.api_key, self.model = api_key, model
        self.url = base_url.rstrip("/") + "/v1/messages"
        self.max_tokens, self.timeout = max_tokens, timeout

    @staticmethod
    def _messages(turns):
        out = []
        for t in turns:
            if t["role"] == "user":
                out.append({"role": "user", "content": t["text"]})
            elif t["role"] == "assistant":
                blocks = []
                if t.get("text"):
                    blocks.append({"type": "text", "text": t["text"]})
                for c in t.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no text)"}]})
            elif t["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     "is_error": bool(r.get("is_error"))}
                    for r in t["results"]
                ]})
        return out

    def complete(self, system, turns, tools):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self._messages(turns),
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                      for t in tools],
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        data = _post(self.url, headers, body, self.timeout)
        text, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append({"id": block["id"], "name": block["name"], "args": block.get("input") or {}})
        return Reply("\n".join(text).strip(), calls, data.get("stop_reason"))


class OpenAICompatibleProvider:
    """OpenAI, Azure-style gateways, Ollama, LM Studio, vLLM, llama.cpp server…"""

    def __init__(self, api_key, model, base_url="https://api.openai.com/v1", max_tokens=4096, timeout=180):
        if not model:
            raise LLMError("COLLAB_AGENT_MODEL must be set for the openai provider")
        self.api_key, self.model = api_key, model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.max_tokens, self.timeout = max_tokens, timeout

    @staticmethod
    def _messages(system, turns):
        out = [{"role": "system", "content": system}]
        for t in turns:
            if t["role"] == "user":
                out.append({"role": "user", "content": t["text"]})
            elif t["role"] == "assistant":
                msg = {"role": "assistant", "content": t.get("text") or None}
                if t.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                        for c in t["tool_calls"]
                    ]
                out.append(msg)
            elif t["role"] == "tool":
                for r in t["results"]:
                    content = ("ERROR: " if r.get("is_error") else "") + r["content"]
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": content})
        return out

    def complete(self, system, turns, tools):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._messages(system, turns),
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                for t in tools],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = _post(self.url, headers, body, self.timeout)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response: {str(data)[:300]}") from None
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = {"_unparsed": raw}
            calls.append({"id": c.get("id") or f"call_{i}", "name": fn.get("name", ""), "args": args or {}})
        return Reply(strip_thinking(msg.get("content") or ""), calls, choice.get("finish_reason"))


def make_provider(cfg):
    if cfg.provider == "anthropic":
        return AnthropicProvider(cfg.api_key, cfg.model or "claude-sonnet-5",
                                 cfg.base_url or "https://api.anthropic.com", cfg.max_tokens)
    if cfg.provider == "openai":
        return OpenAICompatibleProvider(cfg.api_key, cfg.model, cfg.base_url or "https://api.openai.com/v1",
                                        cfg.max_tokens)
    raise LLMError(f"unknown provider {cfg.provider!r} (use anthropic or openai)")
