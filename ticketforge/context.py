"""Bounded model views with recoverable observations and intact tool-call pairs."""
from __future__ import annotations

import copy
import json


class ContextBudgetError(ValueError):
    pass


def size(messages):
    return len(json.dumps(messages, ensure_ascii=False))


def bounded_messages(messages, limit):
    """Canonical task stays intact. Archive results, then complete older exchanges.

    Full observations remain accessible through read_observation. Excerpts are evidence
    fragments, not model-generated interpretations of requirements or success.
    """
    view = copy.deepcopy(messages)
    if size(view) <= limit:
        return view
    for message in view[1:]:
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") != "tool_result":
                continue
            text = block.get("content", "")
            if not isinstance(text, str) or len(text) <= 900:
                continue
            try:
                result = json.loads(text)
            except (ValueError, TypeError):
                result = {}
            reference = result.get("observation_id")
            if not reference:
                continue
            block["content"] = json.dumps({"observation_id": reference, "exit_code": result.get("exit_code"),
                "archived": True, "notice": "Full result available through read_observation. Excerpt is incomplete.",
                "excerpt": text[:300] + "\n...\n" + text[-300:]})
            if size(view) <= limit:
                return view
    archived = []
    while len(view) > 3 and size(view) > limit:
        assistant, results = view[1:3]
        if assistant["role"] != "assistant" or results["role"] != "user":
            raise ContextBudgetError("Unexpected tool history shape; refusing unsafe compaction")
        for block in results.get("content", []):
            try:
                data = json.loads(block.get("content", ""))
                if data.get("observation_id"):
                    archived.append(data["observation_id"])
            except (ValueError, TypeError):
                pass
        del view[1:3]
        view[0]["content"] = (messages[0]["content"] + "\n\nExecution-history notice: older complete tool exchanges were archived. "
            "Reinspect actual files before relying on earlier conclusions. Recover full results using read_observation. "
            "Recent archived observation IDs: " + ", ".join(archived[-20:]))
    if size(view) > limit:
        raise ContextBudgetError("Canonical task or tool request exceeds context budget; narrow the task or raise max_input_chars")
    return view
