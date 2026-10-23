"""Choose the LLM behind the agent: OpenAI (default) or Google Gemini.

Gemini is reached through Google's OpenAI-compatible Chat Completions endpoint
(https://ai.google.dev/gemini-api/docs/openai), so the rest of the stack (MCP
client, guardrails, approvals) is unchanged.

    OPENAI_API_KEY=...                  -> OpenAI, model OPENAI_MODEL or the SDK default
    GEMINI_API_KEY=...                  -> Gemini, model GEMINI_MODEL or DEFAULT_GEMINI_MODEL
    STUDYSPOT_PROVIDER=openai|gemini    -> force a choice when both keys are set
"""

from __future__ import annotations

import copy
import dataclasses
import os
import sys
from typing import Any

from agents import Model, ModelSettings, OpenAIChatCompletionsModel
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.shared import Reasoning

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
# Tried in order when a model is overloaded (503), rate-limited (429) or unavailable (404).
DEFAULT_GEMINI_FALLBACKS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")

# JSON-Schema keywords outside the subset Gemini function declarations document.
# They only guide the model; the MCP server still enforces the full schema on every call.
_DROP = {"examples", "additionalProperties", "default", "multipleOf", "title", "$schema"}


def simplify_schema(schema: Any) -> Any:
    """Return a Gemini-friendly copy of a tool's JSON schema."""
    if isinstance(schema, list):
        return [simplify_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: simplify_schema(v) for k, v in schema.items() if k not in _DROP}
    # anyOf [X, {"type": "null"}]  ->  X with nullable: true
    options = out.get("anyOf")
    if isinstance(options, list):
        non_null = [o for o in options if o.get("type") != "null"]
        if len(non_null) == 1 and len(non_null) < len(options):
            merged = {**non_null[0], **{k: v for k, v in out.items() if k != "anyOf"}, "nullable": True}
            return merged
    return out


class GeminiChatModel(OpenAIChatCompletionsModel):
    """Chat Completions model that sends simplified tool schemas to Gemini and falls back to the
    next model in `fallbacks` when one is overloaded or unavailable."""

    RETRYABLE = {404, 429, 500, 503, 504}

    def __init__(self, model: str, openai_client: AsyncOpenAI, fallbacks: tuple[str, ...] = ()) -> None:
        super().__init__(model=model, openai_client=openai_client)
        self.chain = [model] + [m for m in fallbacks if m != model]

    @classmethod
    def _should_fall_back(cls, err: Exception) -> str | None:
        """Return a short reason if `err` is transient/model-specific, else None (re-raise)."""
        if isinstance(err, APIStatusError):
            return f"HTTP {err.status_code}" if err.status_code in cls.RETRYABLE else None
        if isinstance(err, APIConnectionError):  # includes APITimeoutError
            return "timed out" if "timed out" in str(err).lower() else "connection error"
        return None

    async def get_response(self, system_instructions, input, model_settings, tools, *args, **kwargs):
        tools = [
            dataclasses.replace(t, params_json_schema=simplify_schema(copy.deepcopy(t.params_json_schema)))
            if hasattr(t, "params_json_schema") else t
            for t in tools
        ]
        order = list(self.chain)
        for i, name in enumerate(order):
            self.model = name
            try:
                return await super().get_response(system_instructions, input, model_settings, tools, *args, **kwargs)
            except Exception as err:
                reason = self._should_fall_back(err)
                if reason is None or i == len(order) - 1:
                    raise
                nxt = order[i + 1]
                # Sticky: put the failing model last so later turns start with one that works.
                self.chain = order[i + 1:] + order[:i + 1]
                print(f"  ({name} {reason}; switching to {nxt} for this session)", file=sys.stderr, flush=True)
        raise RuntimeError("unreachable")  # pragma: no cover




def _key(name: str) -> str | None:
    """Read an API key, treating empty values and the .env.example placeholders as unset."""
    value = (os.environ.get(name) or "").strip()
    if not value or "your-key-here" in value or "your-gemini-key-here" in value:
        return None
    return value


def provider() -> str:
    forced = os.environ.get("STUDYSPOT_PROVIDER", "").strip().lower()
    if forced in {"openai", "gemini"}:
        return forced
    if _key("OPENAI_API_KEY"):
        return "openai"
    if _key("GEMINI_API_KEY"):
        return "gemini"
    return "none"


def build_model() -> tuple[Model | str | None, ModelSettings, str]:
    """Return (model, settings, label) for the configured provider."""
    which = provider()
    if which == "gemini":
        key = _key("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("STUDYSPOT_PROVIDER=gemini but GEMINI_API_KEY is not set.")
        name = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        client = AsyncOpenAI(api_key=key, base_url=os.environ.get("GEMINI_BASE_URL", GEMINI_BASE_URL),
                             timeout=float(os.environ.get("STUDYSPOT_MODEL_TIMEOUT", "30")),
                             # 429/503 "high demand" errors are temporary: retry with exponential backoff.
                             max_retries=int(os.environ.get("STUDYSPOT_MODEL_RETRIES", "1")))
        fallbacks = tuple(m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()) \
            or DEFAULT_GEMINI_FALLBACKS
        # parallel_tool_calls is left unset: it is an OpenAI-specific parameter.
        # Light reasoning is enough for tool routing and keeps Gemini 3 responses fast.
        effort = os.environ.get("GEMINI_REASONING_EFFORT", "low")
        settings = ModelSettings(reasoning=Reasoning(effort=effort)) if effort != "default" else ModelSettings()
        return (GeminiChatModel(model=name, openai_client=client, fallbacks=fallbacks), settings,
                f"Gemini ({name}, fallback: {', '.join(fallbacks)})")
    if which == "openai":
        if not _key("OPENAI_API_KEY"):
            raise RuntimeError("STUDYSPOT_PROVIDER=openai but OPENAI_API_KEY is not set.")
        name = os.environ.get("OPENAI_MODEL") or None
        return name, ModelSettings(parallel_tool_calls=False), f"OpenAI ({name or 'SDK default model'})"
    raise RuntimeError("Put a real OPENAI_API_KEY or GEMINI_API_KEY in .env (see README). "
                       "For an offline run use: python scripts/demo_offline.py")
