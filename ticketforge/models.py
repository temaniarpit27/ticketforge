"""Talking to AI services, whichever one you configure.

Each AI service expects requests in its own format. A "session" class hides that: it keeps
the conversation in the service's format and hands the agent loop the same simple tool
calls ({id, name, input}) whatever the service. That is why any model can be the coder or
the reviewer. Three formats cover most services and local servers (config.MODEL_APIS):
Anthropic Messages, OpenAI Responses, and the widely copied OpenAI Chat Completions.
"""
from __future__ import annotations

import json

from .config import secret
from .context import bounded_messages, bounded_turns, chat_result_field
from .integrations import HTTP


def client(spec, max_attempts):
    """HTTP client for one provider spec (from config.provider_specs). Keys stay on the host."""
    headers = dict(spec["headers"])
    key = secret(spec["api_key_env"]) if spec["auth"] != "none" else ""
    if spec["auth"] == "bearer":
        headers["Authorization"] = "Bearer " + key
    elif spec["auth"] in ("x-api-key", "api-key"):
        headers[spec["auth"]] = key
    if spec["api"] == "anthropic-messages":
        headers.setdefault("anthropic-version", "2023-06-01")
    session = SESSIONS[spec["api"]]
    # Asking an AI changes nothing outside, so a failed request can safely be retried.
    return HTTP(spec["base_url"], headers, max_attempts, retryable_posts={session.path}, timeout=spec["timeout_seconds"])


class Turn:
    """One model response: tool calls plus why generation stopped early, if it did."""

    def __init__(self, calls, raw, stopped=None):
        self.calls, self.raw, self.stopped = calls, raw, stopped


class AnthropicSession:
    path = "/messages"

    def __init__(self, http, model, system, tools, task, max_tokens, limit, spec=None):
        self.http, self.model, self.system, self.max_tokens, self.limit = http, model, system, max_tokens, limit
        self.tools = tools
        self.messages = [{"role": "user", "content": task}]
        self.pending = None

    def step(self, guard):
        view = bounded_messages(self.messages, self.limit)
        result = self.http.call("POST", self.path, {
            "model": self.model, "max_tokens": self.max_tokens, "system": self.system,
            "tools": self.tools, "messages": view}, guard=guard)
        stopped = {"max_tokens": "truncated", "refusal": "refused"}.get(result.get("stop_reason"))
        content = result.get("content", [])
        self.pending = content
        calls = [{"id": b["id"], "name": b["name"], "input": b.get("input")}
                 for b in content if b.get("type") == "tool_use"]
        return Turn(calls, result, stopped)

    def pin(self, text):
        """Add text to the task message, which history trimming never removes."""
        self.messages[0]["content"] += "\n\n" + text

    def respond(self, outputs):
        self.messages.append({"role": "assistant", "content": self.pending})
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": o["id"], "content": o["content"], **({"is_error": True} if o["is_error"] else {})}
            for o in outputs]})


def _arguments(text):
    try:
        return json.loads(text or "{}")
    except ValueError:
        return None  # Reported back to the model as a tool error.


class OpenAIResponsesSession:
    """Responses API without server-side storage: every output item is replayed unchanged."""
    path = "/responses"

    def __init__(self, http, model, system, tools, task, max_tokens, limit, spec=None):
        self.http, self.model, self.system, self.max_tokens, self.limit = http, model, system, max_tokens, limit
        self.tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["input_schema"], "strict": False} for t in tools]
        self.task = task
        self.turns = []
        self.pending = None

    def step(self, guard):
        items = bounded_turns(self.task, self.turns, self.limit)
        result = self.http.call("POST", self.path, {
            "model": self.model, "instructions": self.system, "input": items, "tools": self.tools,
            "max_output_tokens": self.max_tokens, "store": False,
            "include": ["reasoning.encrypted_content"]}, guard=guard)
        output = result.get("output", [])
        self.pending = output
        stopped = None
        if result.get("status") == "incomplete":
            stopped = "truncated"
        elif result.get("status") not in (None, "completed"):
            stopped = "failed"
        if any(part.get("type") == "refusal" for item in output if item.get("type") == "message"
               for part in item.get("content", [])):
            stopped = "refused"
        calls = [{"id": item["call_id"], "name": item["name"], "input": _arguments(item.get("arguments"))}
                 for item in output if item.get("type") == "function_call"]
        return Turn(calls, result, stopped)

    def pin(self, text):
        """Add text to the task message, which history trimming never removes."""
        self.task += "\n\n" + text

    def respond(self, outputs):
        self.turns.append(list(self.pending) + [
            {"type": "function_call_output", "call_id": o["id"],
             "output": ("ERROR: " + o["content"]) if o["is_error"] else o["content"]}
            for o in outputs])


class OpenAIChatSession:
    """OpenAI-compatible Chat Completions: the common denominator for many vendors and local servers."""
    path = "/chat/completions"

    def __init__(self, http, model, system, tools, task, max_tokens, limit, spec=None):
        self.http, self.model, self.system, self.max_tokens, self.limit = http, model, system, max_tokens, limit
        self.max_tokens_field = (spec or {}).get("max_tokens_field", "max_tokens")
        self.replay_fields = (spec or {}).get("replay_fields", [])
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools]
        self.task = task
        self.turns = []
        self.pending = None

    def step(self, guard):
        messages = [{"role": "system", "content": self.system}] + bounded_turns(self.task, self.turns, self.limit, chat_result_field)
        result = self.http.call("POST", self.path, {
            "model": self.model, "messages": messages, "tools": self.tools,
            self.max_tokens_field: self.max_tokens}, guard=guard)
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        reason = choice.get("finish_reason")
        stopped = "truncated" if reason == "length" else ("refused" if reason == "content_filter" or message.get("refusal") else None)
        tool_calls = message.get("tool_calls") or []
        # Send back the portable fields, plus any the provider requires (replay_fields). Other
        # vendor extras are dropped, because some services reject fields they do not know.
        self.pending = {"role": "assistant", "content": message.get("content"), **({"tool_calls": tool_calls} if tool_calls else {}),
                        **{name: message[name] for name in self.replay_fields if name in message}}
        calls = [{"id": c["id"], "name": c["function"]["name"], "input": _arguments(c["function"].get("arguments"))}
                 for c in tool_calls if c.get("type", "function") == "function"]
        return Turn(calls, result, stopped)

    def pin(self, text):
        """Add text to the task message, which history trimming never removes."""
        self.task += "\n\n" + text

    def respond(self, outputs):
        self.turns.append([self.pending] + [
            {"role": "tool", "tool_call_id": o["id"], "content": ("ERROR: " + o["content"]) if o["is_error"] else o["content"]}
            for o in outputs])


SESSIONS = {"anthropic-messages": AnthropicSession, "openai-responses": OpenAIResponsesSession, "openai-chat": OpenAIChatSession}
