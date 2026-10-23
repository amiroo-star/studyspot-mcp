"""Text hygiene used on both sides of the MCP boundary.

`clean_text` normalises untrusted free text (removes invisible / bidi / control
characters that are used to hide instructions from a human reviewer).
`find_injection` flags text that reads like instructions to a model.

Pattern matching is a *heuristic* layer. The structural defenses (identity
from the transport, human approval of side effects, tool allow-list, pinned
tool manifest, no cross-user free text) are what actually bound the damage.
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width characters, bidi overrides/isolates, BOM, soft hyphen, tag block.
_INVISIBLE = re.compile(
    "[­​-‏‪-‮⁠-⁤⁦-⁩﻿"
    "\U000e0000-\U000e007f]"
)
_WS = re.compile(r"\s+")

_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|prompt|rule|guideline|previous|above|prior)s?\b",
    r"\b(system|developer)\s*(prompt|message|instruction)s?\b",
    r"\byou\s+are\s+now\b",
    r"\bnew\s+instructions?\b",
    r"\b(act|behave)\s+as\s+(if|an?|the)\b",
    r"\b(call|invoke|run|use)\s+(the\s+)?(tool|function)\b",
    r"\b(book_room|plan_study_session|search_rooms)\s*\(",
    r"<\s*/?\s*(system|assistant|user|tool|instructions?)\s*>",
    r"<\|[a-z_]+\|>",
    r"^\s*(system|assistant|developer)\s*:",
    r"\bjailbreak\b|\bdo anything now\b|\bDAN\b",
    r"https?://|www\.",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.I | re.M)


def clean_text(value: str, *, max_len: int = 120) -> str:
    """NFKC-normalise, strip invisible and control characters, collapse spaces."""
    value = unicodedata.normalize("NFKC", value)
    value = _INVISIBLE.sub("", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in "\t\n ")
    value = _WS.sub(" ", value).strip()
    return value[:max_len]


def find_injection(value: str) -> str | None:
    """Return the first instruction-like fragment found, or None."""
    normalised = _INVISIBLE.sub("", unicodedata.normalize("NFKC", value))
    match = _INJECTION_RE.search(normalised)
    return match.group(0) if match else None
