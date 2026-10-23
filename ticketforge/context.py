"""Keeping each AI conversation under a size limit.

A long run piles up tool output. Before every request we trim the history: first big tool
outputs are swapped for a short note with an ID (the AI can read the full text back with
read_observation), then the oldest whole exchanges are dropped. A tool call is always kept
together with its result, because the AI services reject a call without its answer. The
original task is never trimmed; if it alone is too big, the run stops with a clear error.
"""
from __future__ import annotations

import copy
import json


class ContextBudgetError(ValueError):
    pass


# Sizes are counted in characters of JSON, not tokens: simple and the same for every AI
# service, though only approximate. The system prompt and tool definitions are not counted.
def size(messages):
    return len(json.dumps(messages, ensure_ascii=False))


def archived(text):
    """Replace a large recoverable tool result with a short reference, or None to keep it."""
    if not isinstance(text, str) or len(text) <= 900:
        return None
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        return None
    reference = result.get("observation_id") if isinstance(result, dict) else None
    if not reference:
        return None
    return json.dumps({"observation_id": reference, "exit_code": result.get("exit_code"),
        "archived": True, "notice": "Full result available through read_observation. Excerpt is incomplete.",
        "excerpt": text[:300] + "\n...\n" + text[-300:]})


def history_notice(task, archived_ids):
    return (task + "\n\nExecution-history notice: older complete tool exchanges were archived. "
            "Reinspect actual files before relying on earlier conclusions. Recover full results using read_observation. "
            "Recent archived observation IDs: " + ", ".join(archived_ids[-20:]))


def responses_result_field(item):
    return "output" if item.get("type") == "function_call_output" else None


def chat_result_field(item):
    return "content" if item.get("role") == "tool" else None


def bounded_turns(task, turns, limit, result_field=responses_result_field):
    """OpenAI-style variant: each turn is the model's output plus its tool results.

    Turns are dropped whole, so reasoning items, calls, and results always stay together.
    result_field names the key holding a tool result's text, or None for other items.
    """
    view = copy.deepcopy(turns)
    task_item = {"role": "user", "content": task}

    def flat():
        return [task_item] + [item for turn in view for item in turn]

    if size(flat()) <= limit:
        return flat()
    for turn in view:
        for item in turn:
            field = result_field(item)
            if field is None:
                continue
            stub = archived(item.get(field, ""))
            if stub is None:
                continue
            item[field] = stub
            if size(flat()) <= limit:
                return flat()
    dropped = []
    while len(view) > 1 and size(flat()) > limit:
        for item in view.pop(0):
            field = result_field(item)
            if field is not None:
                try:
                    data = json.loads(item.get(field, ""))
                    if isinstance(data, dict) and data.get("observation_id"):
                        dropped.append(data["observation_id"])
                except (ValueError, TypeError):
                    pass
        task_item = {"role": "user", "content": history_notice(task, dropped)}
    if size(flat()) > limit:
        raise ContextBudgetError("Canonical task or tool request exceeds context budget; narrow the task or raise max_input_chars")
    return flat()


def bounded_messages(messages, limit):
    """Anthropic-format version: keep the task, shrink big results, then drop old exchanges.

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
            stub = archived(block.get("content", ""))
            if stub is None:
                continue
            block["content"] = stub
            if size(view) <= limit:
                return view
    dropped = []
    while len(view) > 3 and size(view) > limit:
        assistant, results = view[1:3]
        if assistant["role"] != "assistant" or results["role"] != "user":
            raise ContextBudgetError("Unexpected tool history shape; refusing unsafe compaction")
        for block in results.get("content", []):
            try:
                data = json.loads(block.get("content", ""))
                if data.get("observation_id"):
                    dropped.append(data["observation_id"])
            except (ValueError, TypeError):
                pass
        del view[1:3]
        view[0]["content"] = history_notice(messages[0]["content"], dropped)
    if size(view) > limit:
        raise ContextBudgetError("Canonical task or tool request exceeds context budget; narrow the task or raise max_input_chars")
    return view
