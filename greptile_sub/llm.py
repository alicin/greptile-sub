"""Talks to any OpenAI-compatible chat API; claude-bridge by default."""

import json
import logging
import os
import time
from typing import Any, Optional

from openai import APIError, OpenAI

from greptile_sub.tools import SPECS, RepoTools

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("GREPTILE_SUB_BASE_URL", "http://127.0.0.1:8000/v1")
API_KEY = os.getenv("GREPTILE_SUB_API_KEY", "none")
TIMEOUT_SECONDS = float(os.getenv("GREPTILE_SUB_LLM_TIMEOUT", "900"))

TOOL_GUIDE = """
You have read-only tools over the PR's checkout: list_files, grep and read_file. Paths are relative \
to the repository root. Call several tools in one reply when you can; each reply costs a round trip. \
When you have seen enough, stop calling tools and say what you concluded."""

_client: Optional[OpenAI] = None


class LLMError(RuntimeError):
    pass


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=TIMEOUT_SECONDS)
    return _client


def _chat(**kwargs) -> Any:
    try:
        return client().chat.completions.create(**kwargs)
    except APIError as e:
        raise LLMError(f"{BASE_URL}: {e}") from e


def complete_json(messages: list[dict[str, Any]], schema: dict[str, Any], name: str, model: str) -> dict[str, Any]:
    response = _chat(
        model=model,
        messages=messages,
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
    )
    content = response.choices[0].message.content or ""
    try:
        out = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"{name}: response is not JSON: {content[:500]}") from e
    missing = [k for k in schema.get("required", []) if k not in out]
    if not isinstance(out, dict) or missing:
        raise LLMError(f"{name}: response is missing {missing}: {content[:500]}")
    return out


def agent_json(
    *,
    instructions: str,
    prompt: str,
    schema: dict[str, Any],
    name: str,
    model: str,
    tools: RepoTools,
    max_steps: int,
) -> dict[str, Any]:
    """Lets the model investigate with tools, then asks for its conclusion as schema-shaped JSON."""
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": instructions + "\n" + TOOL_GUIDE},
        {"role": "user", "content": prompt},
    ]
    for step in range(1, max_steps + 1):
        message = _chat(model=model, messages=messages, tools=SPECS).choices[0].message
        if not message.tool_calls:
            if message.content:
                messages.append({"role": "assistant", "content": message.content})
            break
        messages.append(message.model_dump(exclude_none=True, include={"role", "content", "tool_calls"}))
        for call in message.tool_calls:
            result = tools.call(call.function.name, call.function.arguments)
            logger.debug("%s(%s) -> %d chars", call.function.name, call.function.arguments, len(result))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    else:
        messages.append({"role": "user", "content": "Step limit reached. Stop investigating."})
    messages.append({"role": "user", "content": "Now give your final answer as JSON matching the required schema."})
    out = complete_json(messages, schema, name, model)
    logger.info(
        "%s via %s: %d steps, %d tool calls, %.0fs",
        name, model, step, tools.calls, time.monotonic() - started,
    )
    return out
