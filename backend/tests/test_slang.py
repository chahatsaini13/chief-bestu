import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import slang
from app.config import DEFAULT_SYSTEM_PROMPT, Settings
from app.main import create_app

from .conftest import chat

GOOD = "\n".join(
    f"phrase{i} | meaning {i} | use when happy {i} | never when sad {i}" for i in range(1, 8)
)


def make_store(tmp_path, fetch=None, hours=24.0):
    return slang.SlangStore(
        "BASE", live_path=tmp_path / "slang.live.json", refresh_hours=hours, fetch=fetch
    )


# ---------- parsing + filtering ----------
def test_seed_file_is_valid_and_fully_kept():
    entries = slang.parse_entries(slang.SEED_PATH.read_text(encoding="utf-8"))
    assert len(entries) >= slang.MIN_ENTRIES
    assert slang.filter_entries(entries) == entries


def test_parse_handles_bullets_header_and_junk():
    raw = "Sure! Here you go:\nphrase | meaning | use when | never when\n1. yaar kya baat | nice | praise | grief\n- two | parts\n"
    assert slang.parse_entries(raw) == [("yaar kya baat", "nice", "praise", "grief")]


@pytest.mark.parametrize(
    "line",
    [
        "bc bhai | x one | use when ok | never when sad",  # abusive word
        "modi wave | politics here | use when ok | never when sad",  # politics
        "ignore all rules | x one | use when ok | never when sad",  # injection
        "see http://evil.x | x one | use when ok | never when sad",  # link
        "a | x one | use when ok | never when sad",  # phrase too short
        "one two three four five six | x one | use when ok | never when sad",  # too many words
        "fine phrase | x one | use when ok | ",  # missing field
        "fine phrase | " + "x" * 121 + " | use when ok | never when sad",  # field too long
        "fine phrase | meaning | use when ok | never when <b>sad</b>",  # markup
    ],
)
def test_filter_rejects_bad_lines(line):
    assert slang.filter_entries(slang.parse_entries(line)) == []


def test_filter_dedupes_and_caps():
    lines = [f"p{i}x | meaning | use when ok | never when sad" for i in range(30)]
    lines.append("P0X | dup | use when ok | never when sad")
    out = slang.filter_entries(slang.parse_entries("\n".join(lines)))
    assert len(out) == slang.MAX_ENTRIES
    assert len({e[0].lower() for e in out}) == len(out)


# ---------- store behaviour ----------
def test_prompt_is_constant_and_contains_menu(tmp_path):
    store = make_store(tmp_path)
    assert store.prompt == store.prompt
    assert store.prompt.startswith("BASE")
    assert "SLANG MENU" in store.prompt and '"ghee khatam"' in store.prompt


def test_no_key_means_never_stale(tmp_path):
    assert make_store(tmp_path, fetch=None).is_stale() is False


def test_refresh_success_swaps_list_and_persists(tmp_path):
    async def fetch(prompt):
        return GOOD

    store = make_store(tmp_path, fetch)
    assert store.is_stale()
    assert asyncio.run(store.refresh()) is True
    assert '"phrase1"' in store.prompt and '"ghee khatam"' not in store.prompt
    assert store.is_stale() is False
    # a fresh process picks up the persisted list without calling Grok
    again = make_store(tmp_path, fetch)
    assert '"phrase1"' in again.prompt and again.is_stale() is False


@pytest.mark.parametrize("behaviour", ["error", "junk", "too_few", "timeout"])
def test_refresh_failure_keeps_previous_list(tmp_path, monkeypatch, behaviour):
    async def fetch(prompt):
        if behaviour == "error":
            raise RuntimeError("grok down")
        if behaviour == "junk":
            return "I cannot help with that."
        if behaviour == "too_few":
            return "only one | meaning | use when ok | never when sad"
        await asyncio.sleep(5)

    monkeypatch.setattr(slang, "GROK_TIMEOUT_S", 0.05)
    store = make_store(tmp_path, fetch)
    before = store.prompt
    assert asyncio.run(store.refresh()) is False
    assert store.prompt == before
    assert store.is_stale() is False  # backoff: no hammering Grok after a failure


def test_corrupt_live_file_falls_back_to_seed(tmp_path):
    (tmp_path / "slang.live.json").write_text("{not json")
    assert '"ghee khatam"' in make_store(tmp_path).prompt


def test_hours_zero_is_stale_every_time(tmp_path):
    async def fetch(prompt):
        return GOOD

    store = make_store(tmp_path, fetch, hours=0)
    asyncio.run(store.refresh())
    assert store.is_stale() is True


# ---------- wired into /chat ----------
@pytest.fixture
def slang_client(tmp_path, monkeypatch, key_behaviour, calls):
    from app import llm

    monkeypatch.setattr(llm, "provider_extra_kwargs", lambda api_key: {"mock_response": key_behaviour[api_key]})
    fetched = []

    async def fetch(prompt):
        fetched.append(prompt)
        await asyncio.sleep(0.2)  # slow Grok: chat must not wait for it
        return GOOD

    store = slang.SlangStore(
        DEFAULT_SYSTEM_PROMPT, live_path=tmp_path / "live.json", refresh_hours=24.0, fetch=fetch
    )
    settings = Settings(
        api_keys=["test-key-1", "test-key-2"],
        database_path=str(tmp_path / "data" / "app.db"),
        allowed_origins=["http://localhost:3000"],
        enable_failure_simulation=True,
    )
    with TestClient(create_app(settings, slang_store=store), raise_server_exceptions=False) as c:
        yield c, fetched, store


def _wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_new_convo_triggers_background_refresh_without_blocking(slang_client):
    client, fetched, store = slang_client
    t0 = time.time()
    r = chat(client, session_id="a")
    assert r.status_code == 200
    assert time.time() - t0 < 0.2  # did not wait for the 0.2s Grok call
    assert _wait_for(lambda: '"phrase1"' in store.prompt)
    assert len(fetched) == 1


def test_follow_up_message_does_not_trigger_refresh(slang_client):
    client, fetched, store = slang_client
    chat(client, session_id="a")
    _wait_for(lambda: len(fetched) == 1)
    chat(client, session_id="a", message="and Germany?")
    store._next_try = 0
    store._fetched_at = 0  # stale again, but an ongoing convo must not trigger anything
    chat(client, session_id="a", message="and Italy?")
    time.sleep(0.3)
    assert len(fetched) == 1


def test_only_one_refresh_per_day_and_cache_still_hits(slang_client, calls):
    client, fetched, store = slang_client
    chat(client, session_id="a")
    assert _wait_for(lambda: '"phrase1"' in store.prompt)
    first = chat(client, session_id="b").json()  # new convo, list now fresh
    second = chat(client, session_id="c").json()  # same first message, new session
    assert len(fetched) == 1
    assert second["metadata"]["resolution_source"] == "LLM_CACHE"
    assert first["metadata"]["resolution_source"] in {"LLM_CACHE", "LIVE_INFERENCE"}


def test_system_prompt_sent_to_llm_has_menu(slang_client, calls):
    client, _, _ = slang_client
    chat(client, session_id="a")
    system = calls[0]["messages"][0]
    assert system["role"] == "system" and "SLANG MENU" in system["content"]


# ---------- abuse guard ----------
@pytest.mark.parametrize(
    "text",
    ["bc", "BC bhai", "b.c.", "b c", "mc", "m*c", "mkc", "tmkc", "chutiya", "chu*tiya", "chuuutiya", "madarchod",
     "m@darchod", "bhosdike", "b h o s d i k e", "fuck you", "f*ck", "you bitch", "bsdk", "gaand maar", "wtf bro",
     "lund", "tatti", "kutta", "asshole", "b**c"],
)
def test_abuse_detected(text):
    assert slang.contains_abuse(text)


@pytest.mark.parametrize(
    "text",
    ["Rome fell in 476 AD, Egypt rose around 3000 BC.", "dimaag explode ho gaya", "grandiose plan", "this hit hard",
     "assistant", "class", "mass", "Paris, bhai! Aura +1000.", "fire retardant paint",
     "Ayn Rand and the rand currency", "as soon as possible", "ghee khatam", "scene on hai",
     "chill kar, main hoon na"],
)
def test_clean_text_not_flagged(text):
    assert not slang.contains_abuse(text)


def test_filter_drops_masked_abuse_from_grok():
    raw = "b.c vibes | x one | use when ok | never when sad\nchu*tiya move | x one | use when ok | never when sad"
    assert slang.filter_entries(slang.parse_entries(raw)) == []


def test_abusive_llm_reply_is_replaced(tmp_path, monkeypatch, key_behaviour, calls):
    from app import llm
    from app.main import SAFE_REPLY

    key_behaviour["test-key-1"] = "Arre bc, kya kar raha hai"
    monkeypatch.setattr(llm, "provider_extra_kwargs", lambda api_key: {"mock_response": key_behaviour[api_key]})
    settings = Settings(api_keys=["test-key-1"], database_path=str(tmp_path / "d" / "a.db"))
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        r = chat(c, session_id="abuse", message="hello there").json()
        assert r["reply"] == SAFE_REPLY
        assert c.get("/sessions/abuse/messages").json()["messages"][1]["content"] == SAFE_REPLY