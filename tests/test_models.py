import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from ticketforge.config import Config, provider_specs
from ticketforge.context import ContextBudgetError, bounded_turns, chat_result_field, size
from ticketforge.models import OpenAIChatSession, OpenAIResponsesSession as OpenAISession, client
from ticketforge.runtime import Agents, ExecutionError

SKILLS = Path(__file__).resolve().parents[1] / "ticketforge" / "skills"
TOOLS = [{"name": "shell", "description": "run", "input_schema": {"type": "object", "properties": {}}}]


def call_item(call_id, name, arguments):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id, "name": name, "arguments": arguments}


def recorder(responses):
    requests = []

    def call(method, path, body, **kwargs):
        requests.append((path, copy.deepcopy(body)))
        return responses[len(requests) - 1]
    return SimpleNamespace(call=call), requests


def agents(cfg, clients, sandbox=None):
    value = Agents.__new__(Agents)
    value.cfg, value.clients, value.skill_root = cfg, clients, SKILLS
    value.sandbox = sandbox or SimpleNamespace(run=lambda *a, **kw: {"exit_code": 0, "output": "ok"})
    return value


def cfg(**overrides):
    values = dict(max_agent_steps=5, max_input_chars=100000, max_output_tokens=1000, reviewer_max_output_tokens=25000,
                  coder_model="claude-model", reviewer_model="codex-model", coder_provider="anthropic", reviewer_provider="openai")
    return SimpleNamespace(**{**values, **overrides})


class OpenAISessionTests(unittest.TestCase):
    def test_request_shape_and_stateless_replay(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []}
        http, requests = recorder([
            {"status": "completed", "output": [reasoning, call_item("c1", "shell", '{"command": "ls"}')]},
            {"status": "completed", "output": []},
        ])
        session = OpenAISession(http, "codex-model", "system text", TOOLS, "task text", 25000, 100000)
        turn = session.step(lambda: None)
        self.assertEqual(turn.calls, [{"id": "c1", "name": "shell", "input": {"command": "ls"}}])
        self.assertIsNone(turn.stopped)
        path, body = requests[0]
        self.assertEqual(path, "/responses")
        self.assertEqual((body["model"], body["instructions"], body["store"], body["max_output_tokens"]),
                         ("codex-model", "system text", False, 25000))
        self.assertEqual(body["tools"][0], {"type": "function", "name": "shell", "description": "run",
                                            "parameters": TOOLS[0]["input_schema"], "strict": False})
        self.assertEqual(body["input"], [{"role": "user", "content": "task text"}])
        session.respond([{"id": "c1", "content": "result", "is_error": False}])
        session.step(lambda: None)
        replay = requests[1][1]["input"]
        # Reasoning and call items are replayed unchanged, followed by the matching output.
        self.assertEqual(replay[1], reasoning)
        self.assertEqual(replay[2]["call_id"], "c1")
        self.assertEqual(replay[3], {"type": "function_call_output", "call_id": "c1", "output": "result"})

    def test_stops_and_bad_arguments_are_reported(self):
        cases = [
            ("incomplete", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}, "truncated"),
            ("failed", {"status": "failed", "output": []}, "failed"),
            ("refusal", {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}, "refused"),
        ]
        for name, response, stopped in cases:
            with self.subTest(name):
                http, _ = recorder([response])
                self.assertEqual(OpenAISession(http, "m", "s", TOOLS, "t", 10, 100000).step(lambda: None).stopped, stopped)
        http, _ = recorder([{"status": "completed", "output": [call_item("c1", "shell", "{not json")]}])
        self.assertIsNone(OpenAISession(http, "m", "s", TOOLS, "t", 10, 100000).step(lambda: None).calls[0]["input"])

    def test_error_outputs_are_marked(self):
        http, requests = recorder([{"status": "completed", "output": [call_item("c1", "shell", "{}")]}, {"status": "completed", "output": []}])
        session = OpenAISession(http, "m", "s", TOOLS, "t", 10, 100000)
        session.step(lambda: None)
        session.respond([{"id": "c1", "content": "Unknown tool", "is_error": True}])
        session.step(lambda: None)
        self.assertEqual(requests[1][1]["input"][-1]["output"], "ERROR: Unknown tool")


class BoundedTurnsTests(unittest.TestCase):
    def turn(self, n, output):
        return [call_item(str(n), "shell", "{}"), {"type": "function_call_output", "call_id": str(n), "output": output}]

    def test_archives_then_drops_whole_turns_keeping_task(self):
        big = json.dumps({"observation_id": "obs-1", "exit_code": 1, "output": "x" * 5000})
        turns = [self.turn(1, big), self.turn(2, "y" * 3000), self.turn(3, "small")]
        view = bounded_turns("TASK", turns, 2000)
        self.assertLessEqual(size(view), 2000)
        self.assertTrue(view[0]["content"].startswith("TASK"))
        calls = {i["call_id"] for i in view if i.get("type") == "function_call"}
        outputs = {i["call_id"] for i in view if i.get("type") == "function_call_output"}
        self.assertEqual(calls, outputs)
        self.assertIn("3", calls)

    def test_oversized_task_fails_explicitly(self):
        with self.assertRaises(ContextBudgetError):
            bounded_turns("x" * 5000, [], 4096)


class MixedProviderTests(unittest.TestCase):
    def test_coder_and_reviewer_use_their_own_provider_model_and_limits(self):
        anthropic, anthropic_requests = recorder([{"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "a1", "name": "finish",
            "input": {"status": "ready", "summary": "done", "questions": [], "responses": [], "notes": "", "lessons": []}}]}])
        openai, openai_requests = recorder([{"status": "completed", "output": [call_item("o1", "finish", json.dumps(
            {"status": "pass", "summary": "ok", "questions": [], "findings": [], "resolutions": []}))]}])
        runner = agents(cfg(), {"anthropic": anthropic, "openai": openai})
        self.assertEqual(runner.run("coder", Path("/unused"), {}, lambda: None, lambda e: None)["status"], "ready")
        self.assertEqual(runner.run("reviewer", Path("/unused"), {}, lambda: None, lambda e: None)["status"], "pass")
        self.assertEqual((anthropic_requests[0][1]["model"], anthropic_requests[0][1]["max_tokens"]), ("claude-model", 1000))
        self.assertEqual((openai_requests[0][1]["model"], openai_requests[0][1]["max_output_tokens"]), ("codex-model", 25000))

    def test_finish_hook_can_continue_the_same_conversation(self):
        first = {"status": "pass", "summary": "independent", "questions": [], "findings": [], "resolutions": []}
        final = {**first, "summary": "reconciled", "resolutions": [{"id": "F1", "status": "resolved", "evidence": "fixed"}]}
        openai, requests = recorder([
            {"status": "completed", "output": [call_item("o1", "finish", json.dumps(first))]},
            {"status": "completed", "output": [call_item("o2", "finish", json.dumps(final))]},
        ])
        replies = []

        def hook(result):
            replies.append(result["summary"])
            return {"phase": "reconcile", "prior_findings": [{"id": "F1"}]} if len(replies) == 1 else None

        result = agents(cfg(), {"openai": openai}).run("reviewer", Path("/unused"), {}, lambda: None, lambda e: None, on_finish=hook)
        self.assertEqual(result["summary"], "reconciled")
        self.assertEqual(replies, ["independent", "reconciled"])
        reply = requests[1][1]["input"][-1]
        self.assertEqual(reply["call_id"], "o1")
        self.assertIn("reconcile", reply["output"])

    def test_hook_rejection_is_returned_as_tool_error(self):
        bad = {"status": "ready", "summary": "s", "questions": [], "responses": [], "notes": "", "lessons": []}
        anthropic, requests = recorder([{"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "a1", "name": "finish", "input": bad}]}] * 2)

        def hook(result):
            raise ValueError("responses must cover F1")

        with self.assertRaisesRegex(ExecutionError, "step limit"):
            agents(cfg(max_agent_steps=2), {"anthropic": anthropic}).run("coder", Path("/unused"), {}, lambda: None, lambda e: None, on_finish=hook)
        result = requests[1][1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("cover F1", result["content"])

    def test_non_object_arguments_are_rejected_without_executing(self):
        executed = []
        openai, requests = recorder([{"status": "completed", "output": [call_item("o1", "shell", "[1]")]},
                                     {"status": "incomplete", "output": []}])
        runner = agents(cfg(), {"openai": openai}, SimpleNamespace(run=lambda *a, **kw: executed.append(a)))
        with self.assertRaises(ExecutionError):
            runner.run("reviewer", Path("/unused"), {}, lambda: None, lambda e: None)
        self.assertEqual(executed, [])
        self.assertIn("JSON object", requests[1][1]["input"][-1]["output"])


class ChatSessionTests(unittest.TestCase):
    def test_request_replay_and_stops(self):
        http, requests = recorder([
            {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "reasoning_content": "private",
                "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}}]}}]},
            {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "partial"}}]},
        ])
        session = OpenAIChatSession(http, "any-model", "system text", TOOLS, "task text", 4000, 100000, {"max_tokens_field": "max_completion_tokens"})
        turn = session.step(lambda: None)
        self.assertEqual(turn.calls, [{"id": "t1", "name": "shell", "input": {"command": "ls"}}])
        path, body = requests[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(body["messages"], [{"role": "system", "content": "system text"}, {"role": "user", "content": "task text"}])
        self.assertEqual(body["max_completion_tokens"], 4000)
        self.assertEqual(body["tools"][0]["function"]["name"], "shell")
        session.respond([{"id": "t1", "content": "result", "is_error": False}])
        self.assertEqual(session.step(lambda: None).stopped, "truncated")
        replay = requests[1][1]["messages"]
        self.assertNotIn("reasoning_content", replay[2])
        self.assertEqual(replay[2]["tool_calls"][0]["id"], "t1")
        self.assertEqual(replay[3], {"role": "tool", "tool_call_id": "t1", "content": "result"})

    def test_content_filter_and_refusal_are_refused(self):
        for choice in ({"finish_reason": "content_filter", "message": {}}, {"finish_reason": "stop", "message": {"refusal": "no"}}):
            http, _ = recorder([{"choices": [choice]}])
            self.assertEqual(OpenAIChatSession(http, "m", "s", TOOLS, "t", 10, 100000).step(lambda: None).stopped, "refused")

    def test_chat_compaction_drops_whole_turns(self):
        big = json.dumps({"observation_id": "obs-1", "exit_code": 1, "output": "x" * 5000})
        turns = [[{"role": "assistant", "content": None, "tool_calls": [{"id": str(n)}]},
                  {"role": "tool", "tool_call_id": str(n), "content": {1: big, 2: "y" * 3000, 3: "small"}[n]}] for n in (1, 2, 3)]
        view = bounded_turns("TASK", turns, 2000, chat_result_field)
        self.assertLessEqual(size(view), 2000)
        ids = [m["tool_call_id"] for m in view if m.get("role") == "tool"]
        self.assertEqual(ids, ["3"])


class ProviderSpecTests(unittest.TestCase):
    def test_custom_and_local_providers_and_auth_headers(self):
        specs = provider_specs(SimpleNamespace(model_providers={
            "gemini": {"api": "openai-chat", "base_url": "https://generativelanguage.example/v1beta/openai", "api_key_env": "GEMINI_KEY"},
            "local": {"api": "openai-chat", "base_url": "http://localhost:11434/v1", "auth": "none"},
            "azure": {"api": "openai-chat", "base_url": "https://x.example/openai", "api_key_env": "AZURE_KEY", "auth": "api-key", "headers": {"x-team": "a"}},
        }))
        self.assertEqual(set(specs), {"anthropic", "openai", "gemini", "local", "azure"})
        with patch.dict(os.environ, {"GEMINI_KEY": "g", "AZURE_KEY": "z", "ANTHROPIC_API_KEY": "a"}):
            self.assertEqual(client(specs["gemini"], 1).headers, {"Authorization": "Bearer g"})
            self.assertEqual(client(specs["local"], 1).headers, {})
            self.assertEqual(client(specs["azure"], 1).headers, {"x-team": "a", "api-key": "z"})
            anthropic = client(specs["anthropic"], 1)
            self.assertEqual(anthropic.headers, {"x-api-key": "a", "anthropic-version": "2023-06-01"})
            self.assertEqual(anthropic.retryable_posts, {"/messages"})
            self.assertEqual(anthropic.timeout, 600)

    def test_invalid_provider_specs_are_rejected(self):
        cases = [
            ("unknown_api", {"api": "grpc", "base_url": "https://a/v1", "api_key_env": "K"}, "api must be"),
            ("plain_http_remote", {"api": "openai-chat", "base_url": "http://models.example/v1", "api_key_env": "K"}, "https"),
            ("localhost_lookalike", {"api": "openai-chat", "base_url": "http://localhost.evil.example/v1", "api_key_env": "K"}, "https"),
            ("missing_key_env", {"api": "openai-chat", "base_url": "https://a/v1"}, "api_key_env"),
            ("secret_in_config", {"api": "openai-chat", "base_url": "https://a/v1", "api_key": "sk-123"}, "allowed fields"),
            ("bad_env_name", {"api": "openai-chat", "base_url": "https://a/v1", "api_key_env": "lower"}, "environment variable"),
            ("bad_timeout", {"api": "openai-chat", "base_url": "https://a/v1", "api_key_env": "K", "timeout_seconds": 0}, "timeout"),
        ]
        for name, spec, message in cases:
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                provider_specs(SimpleNamespace(model_providers={"x": spec}))

    def test_same_provider_and_model_for_both_roles_shares_one_client(self):
        http, requests = recorder([
            {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"status": "ready", "summary": "s", "questions": [], "responses": [], "notes": "", "lessons": []})}}]}}]},
            {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "2", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"status": "pass", "summary": "s", "questions": [], "findings": [], "resolutions": []})}}]}}]},
        ])
        shared = cfg(coder_provider="local", reviewer_provider="local", coder_model="same", reviewer_model="same",
                     model_providers={"local": {"api": "openai-chat", "base_url": "http://127.0.0.1:8000/v1", "auth": "none"}})
        runner = agents(shared, {"local": http})
        runner.run("coder", Path("/unused"), {}, lambda: None, lambda e: None)
        runner.run("reviewer", Path("/unused"), {}, lambda: None, lambda e: None)
        self.assertEqual([r[1]["model"] for r in requests], ["same", "same"])
        # Same model, still separate conversations: the reviewer starts fresh.
        self.assertEqual(len(requests[1][1]["messages"]), 2)


class ProviderConfigTests(unittest.TestCase):
    def make(self, temp, **overrides):
        (Path(temp) / ".git").mkdir(exist_ok=True)
        return Config(repo=temp, jira_url="https://jira.example", jira_jql="project = APP", allowed_jira_accounts=["human"],
                      github_repo="org/repo", coder_model="a", reviewer_model="b", verification_commands=["test"], **overrides)

    def test_unknown_provider_rejected_and_secrets_follow_providers(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "Unknown model provider"):
                self.make(temp, reviewer_provider="gemini").validate()
            self.assertEqual(self.make(temp).required_secrets()[-1:], ["ANTHROPIC_API_KEY"])
            self.assertEqual(self.make(temp, reviewer_provider="openai").required_secrets()[-2:], ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
            local = {"local": {"api": "openai-chat", "base_url": "http://localhost:11434/v1", "auth": "none"}}
            both_local = self.make(temp, coder_provider="local", reviewer_provider="local", model_providers=local)
            both_local.validate()
            self.assertEqual(both_local.required_secrets(), ["JIRA_EMAIL", "JIRA_API_TOKEN", "GITHUB_TOKEN"])


if __name__ == "__main__":
    unittest.main()
