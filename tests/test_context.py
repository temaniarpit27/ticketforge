import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from ticketforge.context import bounded_messages, ContextBudgetError, size
from ticketforge.runtime import Agents


class ContextTests(unittest.TestCase):
    def history(self, count=10):
        messages = [{"role": "user", "content": '{"requirement":"exact canonical specification"}'}]
        for n in range(count):
            messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(n), "name": "shell", "input": {"command": "go test ./..."}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(n), "content": json.dumps({"observation_id": f"obs-{n}", "exit_code": 0, "output": "x" * 24000})}]}]
        return messages

    def test_verbose_outputs_are_archived_with_task_and_tool_pairs_preserved(self):
        messages = self.history()
        original = copy.deepcopy(messages)
        view = bounded_messages(messages, 9000)
        self.assertLessEqual(size(view), 9000)
        self.assertTrue(view[0]["content"].startswith(messages[0]["content"]))
        self.assertEqual(messages, original)
        for a, b in zip(view[1::2], view[2::2]):
            self.assertEqual(a["content"][0]["id"], b["content"][0]["tool_use_id"])
        self.assertIn("observation_id", json.dumps(view))

    def test_oversized_canonical_requirements_fail_explicitly(self):
        with self.assertRaises(ContextBudgetError):
            bounded_messages([{"role": "user", "content": "x" * 5000}], 4096)

    def test_agent_can_recover_full_archived_evidence_in_chunks(self):
        agents = Agents.__new__(Agents)
        agents.cfg = SimpleNamespace(max_agent_steps=5, max_input_chars=6000, max_output_tokens=1000, reviewer_max_output_tokens=1000,
                                     coder_model="coder", reviewer_model="reviewer", coder_provider="anthropic", reviewer_provider="anthropic")
        agents.skill_root = Path(__file__).resolve().parents[1] / "ticketforge" / "skills"
        agents.sandbox = SimpleNamespace(run=lambda *a, **kw: {"exit_code": 1, "output": "x" * 22000 + "IMPORTANT_END"})
        requests = []
        def call(method, path, body, **kwargs):
            requests.append(copy.deepcopy(body))
            n = len(requests)
            name, args = ("shell", {"command": "go test ./..."}) if n == 1 else (
                ("read_observation", {"id": "obs-0-1", "offset": 22000}) if n == 2 else
                ("finish", {"status": "blocked", "summary": "Known failing check", "questions": [], "responses": [], "notes": "", "lessons": []}))
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": str(n), "name": name, "input": args}]}
        agents.clients = {"anthropic": SimpleNamespace(call=call)}
        result = agents.run("coder", Path("/unused"), {"specification": "Keep requirements intact"}, lambda: None, lambda e: None)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(all(size(r["messages"]) <= 6000 for r in requests))
        self.assertIn("IMPORTANT_END", json.dumps(requests[-1]["messages"]))
