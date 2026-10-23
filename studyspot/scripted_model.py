"""A deterministic stand-in for the LLM, used by tests and the offline demo.

It implements the Agents SDK `Model` interface, so everything after the model
(tool routing, MCP client, stdio transport, FastMCP server, guardrails,
approvals) is the real code path. Each script step is either a tool call or a
final answer; a step may be a function of the tool output just received.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Callable, Union

from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

Call = tuple[str, dict[str, Any]]
Step = Union[Call, str, Callable[[str], Union[Call, str]]]


def _flatten(out: Any) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        return str(out.get("text", json.dumps(out, default=str)))
    if isinstance(out, list):
        return " ".join(_flatten(o) for o in out)
    return str(out)


class ScriptedModel(Model):
    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.seen_tools: list[str] = []
        self.last_tool_output: str = ""

    @staticmethod
    def _last_output(items: Any) -> str:
        if isinstance(items, list):
            for item in reversed(items):
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    return _flatten(item.get("output"))
        return ""

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id=None, conversation_id=None,
                           prompt=None) -> ModelResponse:
        self.seen_tools = [t.name for t in tools]
        self.last_tool_output = self._last_output(input)
        step = self.steps.pop(0) if self.steps else "Done."
        if callable(step):
            step = step(self.last_tool_output)
        if isinstance(step, str):
            item = ResponseOutputMessage(
                id=f"msg_{uuid.uuid4().hex[:8]}", type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=step, annotations=[])],
            )
        else:
            name, args = step
            item = ResponseFunctionToolCall(
                id=f"fc_{uuid.uuid4().hex[:8]}", call_id=f"call_{uuid.uuid4().hex[:8]}",
                type="function_call", name=name, arguments=json.dumps(args), status="completed",
            )
        return ModelResponse(output=[item], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs) -> AsyncIterator[Any]:  # pragma: no cover
        raise NotImplementedError("ScriptedModel does not stream")
