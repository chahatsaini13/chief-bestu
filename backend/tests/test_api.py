import litellm
import pytest

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

from .conftest import chat

META_FIELDS = {"resolution_source", "model", "latency_ms", "input_tokens", "output_tokens", "key_id", "fallback_attempts"}


# ---------- health ----------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


# ---------- validation (422) ----------
@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s-1"},  # missing message
        {"message": "hi"},  # missing session_id
        {"session_id": "s-1", "message": ""},
        {"session_id": "s-1", "message": "    \n  "},  # only whitespace
        {"session_id": "", "message": "hi"},
        {"session_id": "x" * 129, "message": "hi"},
        {"session_id": "s-1", "message": "a" * 4001},
    ],
)
def test_validation_422(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_malformed_json_422(client):
    r = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_message_exactly_4000_ok_and_trimmed(client):
    assert chat(client, message="  " + "a" * 4000 + "  ").status_code == 200


def test_422_not_saved(client):
    chat(client, session_id="s-bad", message="   ")
    assert client.get("/sessions/s-bad/messages").status_code == 404


# ---------- history ----------
def test_unknown_session_404(client):
    r = client.get("/sessions/nope/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_history_oldest_first_and_no_system_prompt(client):
    chat(client, message="first")
    chat(client, message="second")
    body = client.get("/sessions/s-1/messages").json()
    assert body["session_id"] == "s-1"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in body["messages"] if m["role"] == "user"] == ["first", "second"]
    assert "system" not in roles
    assert "metadata" in body["messages"][1] and "metadata" not in body["messages"][0]
    assert {"resolution_source", "input_tokens", "output_tokens"} <= set(body["messages"][1]["metadata"])


# ---------- contract shape + tokens ----------
def test_chat_contract_shape(client):
    r = chat(client)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s-1" and body["reply"]
    meta = body["metadata"]
    assert META_FIELDS <= set(meta)
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/gemini-2.0-flash"
    assert isinstance(meta["latency_ms"], int)
    assert isinstance(meta["input_tokens"], int) and isinstance(meta["output_tokens"], int)
    assert meta["key_id"] == "key_1" and meta["fallback_attempts"] == 0


def test_tokens_are_per_request_not_running_total(client):
    a = chat(client, session_id="s-a", message="hello there").json()["metadata"]
    b = chat(client, session_id="s-b", message="hello there").json()["metadata"]
    c2 = chat(client, session_id="s-a", message="and again").json()["metadata"]
    assert a["input_tokens"] == b["input_tokens"]
    assert c2["output_tokens"] <= a["output_tokens"] * 2  # never a growing total


# ---------- cache ----------
def test_same_first_message_new_session_hits_cache(client, calls):
    live = chat(client, session_id="s-1", message="What is the capital of France?").json()
    hit = chat(client, session_id="s-2", message="What is the capital of France?").json()
    assert live["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"
    assert hit["reply"] == live["reply"]
    assert hit["metadata"]["key_id"] is None
    assert hit["metadata"]["fallback_attempts"] == 0
    assert hit["metadata"]["input_tokens"] == live["metadata"]["input_tokens"]
    assert hit["metadata"]["output_tokens"] == live["metadata"]["output_tokens"]
    assert isinstance(hit["metadata"]["latency_ms"], int)


def test_different_message_never_from_cache(client):
    chat(client, session_id="s-1", message="What is the capital of France?")
    other = chat(client, session_id="s-2", message="What is the capital of Spain?").json()
    assert other["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_shared_across_keys(client, key_behaviour):
    """A reply produced by key_2 (after key_1 failed) must still be a hit later."""
    r1 = chat(client, session_id="s-1", message="cross key", headers={"X-Simulate-Failure": "key_1=429"}).json()
    assert r1["metadata"]["key_id"] == "key_2"
    # the simulated request skipped the cache READ but still WROTE the answer.
    # A normal request (which would use key_1) must be a hit: api_key is not in the cache key.
    hit = chat(client, session_id="s-2", message="cross key").json()
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"
    assert hit["metadata"]["key_id"] is None
    assert hit["reply"] == r1["reply"]


def test_custom_system_prompt_changes_cache_key(client):
    chat(client, session_id="s-1", message="same text")
    other = chat(client, session_id="s-2", message="same text", system_prompt="You are a pirate.").json()
    assert other["metadata"]["resolution_source"] == "LIVE_INFERENCE"


# ---------- multi-turn + isolation ----------
def test_multi_turn_sends_history_to_llm(client, calls):
    chat(client, session_id="s-m", message="my name is Chahat")
    chat(client, session_id="s-m", message="what is my name?")
    sent = calls[-1]["messages"]
    assert sent[0]["role"] == "system"
    assert [m["content"] for m in sent[1:-1]][0] == "my name is Chahat"
    assert sent[-1] == {"role": "user", "content": "what is my name?"}
    assert len(sent) == 1 + 2 + 1  # system + (user, assistant) + new user


def test_sessions_are_isolated(client, calls):
    chat(client, session_id="s-a", message="secret in A")
    chat(client, session_id="s-b", message="hello from B")
    sent = calls[-1]["messages"]
    assert all("secret in A" not in m["content"] for m in sent)
    a = client.get("/sessions/s-a/messages").json()["messages"]
    b = client.get("/sessions/s-b/messages").json()["messages"]
    assert len(a) == 2 and len(b) == 2


def test_session_system_prompt_persists(client, calls):
    chat(client, session_id="s-p", message="one", system_prompt="You are a pirate.")
    chat(client, session_id="s-p", message="two")
    assert calls[-1]["messages"][0]["content"] == "You are a pirate."


# ---------- failover ----------
@pytest.mark.parametrize("header", ["key_1=429", "key_1=500", "key_1=502", "key_1=503"])
def test_simulated_failover_to_key_2(client, header):
    r = chat(client, session_id=f"s-{header}", headers={"X-Simulate-Failure": header})
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"]


def test_simulated_failover_even_when_message_is_cached(client):
    chat(client, session_id="s-1", message="cached one")
    r = chat(client, session_id="s-2", message="cached one", headers={"X-Simulate-Failure": "key_1=429"})
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_2"


def test_both_keys_fail_specific(client):
    r = chat(client, headers={"X-Simulate-Failure": "key_1=429,key_2=500"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_all_fail_503_and_nothing_saved(client):
    r = chat(client, session_id="s-fail", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/s-fail/messages").status_code == 404


def test_503_does_not_touch_existing_history(client):
    chat(client, session_id="s-keep", message="before")
    chat(client, session_id="s-keep", message="during outage", headers={"X-Simulate-Failure": "*=500"})
    assert len(client.get("/sessions/s-keep/messages").json()["messages"]) == 2


def test_real_provider_failure_falls_over(client, key_behaviour):
    """Not simulated: key_1's provider genuinely raises a 429-style error."""
    key_behaviour["test-key-1"] = litellm.RateLimitError(message="quota", llm_provider="gemini", model="m")
    r = chat(client, message="real failure path")
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_2"
    assert r.json()["metadata"]["fallback_attempts"] == 1


def test_real_failure_of_all_keys_is_clean_503(client, key_behaviour):
    err = litellm.InternalServerError(message="boom", llm_provider="gemini", model="m")
    key_behaviour["test-key-1"] = err
    key_behaviour["test-key-2"] = err
    r = chat(client, message="everything is down")
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_simulation_header_ignored_when_disabled(tmp_path, monkeypatch, key_behaviour):
    from app import llm

    monkeypatch.setattr(llm, "provider_extra_kwargs", lambda k: {"mock_response": key_behaviour[k]})
    s = Settings(api_keys=["test-key-1", "test-key-2"], database_path=str(tmp_path / "d.db"),
                 enable_failure_simulation=False)
    with TestClient(create_app(s), raise_server_exceptions=False) as c:
        r = chat(c, headers={"X-Simulate-Failure": "*=429"})
        assert r.status_code == 200
        assert r.json()["metadata"]["key_id"] == "key_1"


def test_cors_allows_frontend_origin(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
