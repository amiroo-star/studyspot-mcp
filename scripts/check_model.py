"""Step-by-step check of the LLM connection (no MCP involved).

    python scripts/check_model.py

1. which provider and model are configured
2. a plain chat request
3. a request that includes the three StudySpot tool schemas (as the agent sends them)
Each step prints its time and the exact error if it fails.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402

from studyspot import providers  # noqa: E402
from studyspot.host import load_dotenv  # noqa: E402
from studyspot.server import mcp  # noqa: E402


def step(n, text):
    print(f"\n[{n}] {text}", flush=True)


async def main():
    load_dotenv()
    step(1, "Configuration")
    which = providers.provider()
    print("    provider:", which)
    if which != "gemini":
        print("    This checker targets Gemini. Put GEMINI_API_KEY in .env (OpenAI users can skip this script).")
        return
    import os
    from openai import AsyncOpenAI
    model = os.environ.get("GEMINI_MODEL") or providers.DEFAULT_GEMINI_MODEL
    print("    model:", model)
    client = AsyncOpenAI(api_key=providers._key("GEMINI_API_KEY"),
                         base_url=os.environ.get("GEMINI_BASE_URL", providers.GEMINI_BASE_URL),
                         timeout=60, max_retries=0)

    step(2, "Plain chat request")
    t = time.time()
    try:
        r = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Reply with: ok"}])
        print(f"    OK in {time.time() - t:.1f}s ->", (r.choices[0].message.content or "").strip()[:80])
    except Exception as e:
        print(f"    FAILED after {time.time() - t:.1f}s: {type(e).__name__}: {e}")
        if "404" in str(e) or "not found" in str(e).lower():
            print("    -> Model name not available for your key. Set GEMINI_MODEL in .env to a model listed at "
                  "https://ai.google.dev/gemini-api/docs/models")
        elif "401" in str(e) or "403" in str(e) or "API key" in str(e):
            print("    -> Key rejected. Re-copy it from https://aistudio.google.com/apikey into .env.")
        elif "429" in str(e):
            print("    -> Rate limit / quota. Wait a minute and retry.")
        return

    step(3, "Request with the three StudySpot tools")
    async with Client(mcp) as c:
        tools = await c.list_tools()
    specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
              "parameters": providers.simplify_schema(t.input_schema)}} for t in tools]
    t = time.time()
    try:
        r = await client.chat.completions.create(
            model=model, tools=specs, tool_choice="auto",
            messages=[{"role": "user", "content": "Which rooms are free on 2026-12-01 at 14:00 for 4 people for 60 minutes?"}])
        msg = r.choices[0].message
        calls = [(tc.function.name, json.loads(tc.function.arguments)) for tc in (msg.tool_calls or [])]
        print(f"    OK in {time.time() - t:.1f}s -> tool calls: {calls or 'none'}; text: {(msg.content or '')[:80]!r}")
        print("\nAll checks passed. `python -m studyspot.host` should work.")
    except Exception as e:
        print(f"    FAILED after {time.time() - t:.1f}s: {type(e).__name__}: {e}")
        print("    -> Paste this output to get the schema adjusted.")


if __name__ == "__main__":
    asyncio.run(main())
