import litellm
import pytest
from fastapi.testclient import TestClient

from app import llm
from app.config import Settings
from app.main import create_app


@pytest.fixture
def key_behaviour():
    """api_key -> mock_response. A string = success text, an Exception = that provider error."""
    return {"test-key-1": "Paris, bhai! Aura +1000.", "test-key-2": "Paris, bhai! Aura +1000."}


@pytest.fixture
def calls(monkeypatch):
    """Spy on every real litellm.acompletion call (cache hits never reach the provider, but
    they DO pass through acompletion, so we record the message lists that were sent)."""
    seen = []
    real = litellm.acompletion

    async def spy(**kwargs):
        seen.append({"api_key": kwargs.get("api_key"), "messages": kwargs.get("messages")})
        return await real(**kwargs)

    monkeypatch.setattr(litellm, "acompletion", spy)
    return seen


@pytest.fixture
def client(tmp_path, monkeypatch, key_behaviour, calls):
    monkeypatch.setattr(llm, "provider_extra_kwargs", lambda api_key: {"mock_response": key_behaviour[api_key]})
    settings = Settings(
        api_keys=["test-key-1", "test-key-2"],
        database_path=str(tmp_path / "data" / "app.db"),
        allowed_origins=["http://localhost:3000"],
        enable_failure_simulation=True,
    )
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def chat(client, session_id="s-1", message="What is the capital of France?", headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})
