"""Gemini path: provider selection, schema simplification, and a full agent run against a local
stand-in for Google's OpenAI-compatible endpoint (records exactly what would be sent to Gemini)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

from studyspot import providers
from studyspot.host import build_agent, build_server, connect_verified, run_turn

from .conftest import TOMORROW, USER, booking_count


def test_provider_selection(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "STUDYSPOT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    assert providers.provider() == "none"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-your-key-here")       # template placeholder is ignored
    assert providers.provider() == "none"
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert providers.provider() == "gemini"
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert providers.provider() == "openai"
    monkeypatch.setenv("STUDYSPOT_PROVIDER", "gemini")
    assert providers.provider() == "gemini"
    with pytest.raises(RuntimeError):
        monkeypatch.delenv("GEMINI_API_KEY")
        providers.build_model()


def test_schema_simplification():
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "start": {"type": "string", "pattern": "^x$", "examples": ["x"], "description": "d"},
        "n": {"type": "integer", "multipleOf": 30, "minimum": 30},
        "building": {"anyOf": [{"type": "string", "enum": ["A"]}, {"type": "null"}], "default": None,
                     "description": "b"},
        "features": {"type": "array", "items": {"type": "string", "enum": ["quiet"]}, "default": []}}}
    out = providers.simplify_schema(schema)
    text = json.dumps(out)
    for banned in ("additionalProperties", "examples", "multipleOf", "default", "anyOf"):
        assert banned not in text
    assert out["properties"]["building"] == {"type": "string", "enum": ["A"], "description": "b", "nullable": True}
    assert out["properties"]["start"]["pattern"] == "^x$"


class _FakeGemini(BaseHTTPRequestHandler):
    replies: list = []
    requests: list = []

    overloaded: set = set()
    slow: set = set()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if body["model"] in type(self).slow:
            import time
            time.sleep(2.5)
        if body["model"] in type(self).overloaded:
            data = json.dumps({"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        msg = type(self).replies.pop(0)
        payload = {"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                   "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_gemini(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGemini)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for k in ("OPENAI_API_KEY", "STUDYSPOT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_BASE_URL", f"http://127.0.0.1:{srv.server_port}/v1beta/openai/")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("STUDYSPOT_MODEL_RETRIES", "0")
    _FakeGemini.requests = []
    _FakeGemini.overloaded = set()
    _FakeGemini.slow = set()
    yield _FakeGemini
    srv.shutdown()


async def test_agent_runs_on_gemini_endpoint(env, fake_gemini):
    args = dict(date=TOMORROW, start="14:00", duration_minutes=60, group_size=4)
    fake_gemini.replies = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search_rooms", "arguments": json.dumps(args)}}]},
        {"role": "assistant", "content": "7 rooms are free."},
    ]
    server = build_server(USER, str(env))
    await connect_verified(server)
    try:
        model, settings, label = providers.build_model()
        assert label.startswith("Gemini")
        reply, _ = await run_turn(build_agent(server, USER, model, settings), [], "free rooms?", None)
    finally:
        await server.cleanup()

    assert reply == "7 rooms are free." and booking_count() == 0
    first, second = fake_gemini.requests
    assert first["path"] == "/v1beta/openai/chat/completions"
    assert first["auth"] == "Bearer test-gemini-key"
    assert first["body"]["model"] == providers.DEFAULT_GEMINI_MODEL
    assert "parallel_tool_calls" not in first["body"]
    sent = {t["function"]["name"]: t["function"]["parameters"] for t in first["body"]["tools"]}
    assert set(sent) == {"search_rooms", "book_room", "plan_study_session"}
    assert "additionalProperties" not in json.dumps(sent) and "examples" not in json.dumps(sent)
    # The real MCP result went back to the model as the tool message.
    tool_msg = next(m for m in second["body"]["messages"] if m["role"] == "tool")
    assert '"count":7' in json.dumps(tool_msg["content"]).replace("\\", "").replace(" ", "")


async def test_falls_back_to_next_model_when_overloaded(env, fake_gemini):
    fake_gemini.overloaded = {providers.DEFAULT_GEMINI_MODEL}
    fake_gemini.replies = [{"role": "assistant", "content": "hello from fallback"}]
    model, settings, _ = providers.build_model()
    server = build_server(USER, str(env))
    await connect_verified(server)
    try:
        reply, _ = await run_turn(build_agent(server, USER, model, settings), [], "hi", None)
    finally:
        await server.cleanup()
    assert reply == "hello from fallback"
    assert [r["body"]["model"] for r in fake_gemini.requests] == [
        providers.DEFAULT_GEMINI_MODEL, providers.DEFAULT_GEMINI_FALLBACKS[0]]


async def test_timeout_falls_back_and_sticks(env, fake_gemini, monkeypatch):
    monkeypatch.setenv("STUDYSPOT_MODEL_TIMEOUT", "1")
    fake_gemini.slow = {providers.DEFAULT_GEMINI_MODEL}
    fake_gemini.replies = [{"role": "assistant", "content": "one"}, {"role": "assistant", "content": "two"}]
    model, settings, _ = providers.build_model()
    assert settings.reasoning.effort == "low"
    server = build_server(USER, str(env))
    await connect_verified(server)
    try:
        agent = build_agent(server, USER, model, settings)
        first, hist = await run_turn(agent, [], "hi", None)
        second, _ = await run_turn(agent, hist, "again", None)
    finally:
        await server.cleanup()
    assert (first, second) == ("one", "two")
    models = [r["body"]["model"] for r in fake_gemini.requests]
    fb = providers.DEFAULT_GEMINI_FALLBACKS[0]
    # slow model tried (timeout + 0 retries), then fallback; second turn goes straight to the fallback
    assert models[-1] == fb and models[-2] == fb and providers.DEFAULT_GEMINI_MODEL in models
    assert fake_gemini.requests[-1]["body"]["reasoning_effort"] == "low"
