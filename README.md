# Chief Bestu

**Designed and built by Chahat Saini.**

A small, reliable AI chat service with a personality of its own: a **CEO who gets things done *and* your bestfriend who keeps you company**.
Built end to end: React + FastAPI + SQLite, with LiteLLM for the model calls and cache, automatic API-key failover,
per-request metadata on every reply, Docker, and a live deployment.

## What makes Chief Bestu different

Most chatbots feel like a form with a text box. Chief Bestu is built around a character, and the character is the product.

- **A real persona.** Part CEO (answers first, keeps you on track), part bestfriend (warm, funny, a little playful). If you are
  genuinely sad or stressed, the jokes stop and it becomes a calm, kind friend.
- **Speaks your language.** It replies in plain English by default and switches to Hinglish *only if you write in Hinglish first*.
- **Make it yours.**
  - **Theme:** Pink, Lavender, Mint or Night, from the `style` button in the UI.
  - **Font:** a retro pixel font or an easy-to-read clean font, also from `style`. Choices are remembered in the browser.
  - **Personality:** open `boardroom settings` and write your own system prompt to change how the bot behaves for that
    session (a strict tutor, a pirate, a calm coach, anything you like). Leave it empty to get the default Chief Bestu.
- **A look that is not "yet another AI chatbot":** a pastel retro `chat.exe` window with pixel borders, instead of a standard chat UI.

## 1. Live URLs

| What | URL |
|---|---|
| Frontend | https://chief-bestu-lks6.vercel.app/ |
| Backend | https://chief-bestu.onrender.com |
| Backend health | https://chief-bestu.onrender.com/health |
| Demo video | `<paste video link here>` |

> The backend runs on a free tier and sleeps when idle. The first request after a pause can take about a minute.
> The free tier also has no persistent disk, so history and cache can reset when the service restarts.

## 2. Run locally

### With Docker (recommended)

```bash
cp .env.example .env        # then put 2+ real Gemini keys in LLM_API_KEYS
make up                     # backend -> http://localhost:8000 , frontend -> http://localhost:3000
make down
```

SQLite lives on the named volume `app_data`, mounted at `/data`, so history survives container restarts.

### Without Docker

```bash
# backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export LLM_API_KEYS=key1,key2 DATABASE_URL=sqlite:///./app.db ALLOWED_ORIGINS=http://localhost:3000 ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --port 8000
python -m pytest -q          # backend tests (LLM mocked)

# frontend (second terminal)
cd frontend
npm install
VITE_API_URL=http://localhost:8000 npm run dev      # http://localhost:3000
npm test                                            # UI contract tests
```

Environment variables (see `.env.example`): `LLM_API_KEYS`, `DATABASE_URL`, `ALLOWED_ORIGINS`,
`ENABLE_FAILURE_SIMULATION`, `MODEL`, `REQUEST_TIMEOUT`, and `VITE_API_URL` (frontend, build time).

## 3. Design decisions

**Key fallback.** `LLM_API_KEYS` becomes a pool with ids `key_1, key_2, ...`. LiteLLM makes the model call and owns the
cache; a small loop in `backend/app/llm.py` owns *per-key* failover. I wrote the loop myself (instead of only using
LiteLLM's Router) because the API contract needs to report exactly which key answered (`key_id`) and how many keys failed
first (`fallback_attempts`). Any provider failure (429, quota, 5xx, timeout, connection error) moves on to the next key.
If every key fails the API returns a clean `503 {"detail": "all providers unavailable"}` and nothing is written to history.
Keys are never logged or returned, only their ids.

**Failure simulation.** `X-Simulate-Failure` (only when `ENABLE_FAILURE_SIMULATION=true`) makes chosen keys raise the *same
LiteLLM exception classes* a real provider would (`RateLimitError`, `InternalServerError`, ...), so a simulated failure
travels through exactly the same failover code as a real one: there is no separate fake branch. While a simulation is
active the cache *read* is skipped, so failover can be demonstrated even for a message that is already cached.

**Caching.** LiteLLM's built-in in-memory cache. The cache key is the model plus the full message list (system prompt +
history + new message); the API key is not part of it, so an answer produced by `key_2` is still a hit for `key_1`.
The default system prompt is a constant with no dates or session ids, so the same first message in a new session
produces the identical message list and hits the cache. A hit returns the identical reply, the token usage stored with
the cached response, `key_id: null` and `fallback_attempts: 0`. Hits are detected from LiteLLM's `cache_hit` flag.
Trade-off: the cache is in memory, so it resets when the backend restarts.

**SQLite session storage.** `messages` follows the required schema (plus an index on `(session_id, id)`), and a small
`sessions` table stores the optional per-session system prompt. The schema is created on startup and the parent folder of
`DATABASE_URL` is created if missing. The request flow is: validate, load history, call the model through the key pool,
and only after success write the user and assistant rows in one transaction. A failed request (422 or 503) therefore never
touches history. Token counts are taken from each response's own usage, so they are per request and never a running total.

**Errors.** Every error is JSON with `detail`: 422 for validation (empty or whitespace-only message, more than 4000
characters, missing fields, malformed JSON), 404 for an unknown session, 503 when all keys fail. A global handler turns
anything unexpected into a JSON 500 without a stack trace.

## 4. Verification

Set `API` to the backend URL (`http://localhost:8000` locally).

```bash
API=http://localhost:8000
curl -s $API/health
```

**Cache hit** (same message in two different sessions; the second is `LLM_CACHE`, `key_id: null`, no model call):

```bash
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s-a","message":"What is the capital of France?"}'
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s-b","message":"What is the capital of France?"}'
```

**Failover** (needs `ENABLE_FAILURE_SIMULATION=true`):

```bash
# key_1 fails -> key_2 answers: HTTP 200, key_id "key_2", fallback_attempts 1
curl -s -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"s-f","message":"Tell me a joke"}'
# every key fails -> HTTP 503 {"detail":"all providers unavailable"}, nothing saved
curl -s -i -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"s-g","message":"Tell me a joke"}'
curl -s -i $API/sessions/s-g/messages        # 404 session not found
```

**Per-message token usage:** every `/chat` response (and every reply in the UI) carries `input_tokens` and
`output_tokens` for that request only. Send two different first messages of equal length in two new sessions and the
input tokens are about the same; they do not grow across turns as a total.

**Multi-turn and history:**

```bash
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s-h","message":"My name is Chahat"}'
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s-h","message":"What is my name?"}'
curl -s $API/sessions/s-h/messages
```

**Validation:** `curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s-v","message":"   "}'` returns 422.

**Automated:** `python -m pytest -q` in `backend/` runs the backend suite (101 tests, LLM mocked, real LiteLLM cache);
`npm test` in `frontend/` checks the `data-testid` contract and error rendering.

## 5. Setup Used

- Editor: VS Code
- AI tools: Claude (Anthropic) for scaffolding, code and tests; I reviewed and ran everything myself and can explain each part.
- Stack: React (Vite), FastAPI, SQLite (aiosqlite), LiteLLM, Docker, Render (backend), Vercel (frontend).

## 6. Bonus features

- **Chief Bestu persona:** a constant, cache-friendly system prompt (a CEO who directs + a bestfriend with a playful voice,
  English by default, Hinglish only when the user starts it, serious and kind when the user is genuinely struggling).
- **Personalisation in the UI:** four colour themes, a pixel or easy-to-read font (saved in the browser), and an editable
  system prompt per session so users can change the bot's personality if they like.
- **Retro `chat.exe` design** with pixel borders and sticker-style metadata chips for source, model, latency, tokens, key and fallbacks.
- **Failure simulation panel in the UI** (`boardroom settings`) so failover can be shown without curl.
- **Frontend tests** for the UI contract, and a `scripts/smoke_live.py` script to verify real keys, model and cache before deploying.
- **Docker healthcheck** on the backend and a non-root runtime user.
- **Automatic slang refresh.** The slang menu lives in `backend/app/slang.txt` and is only used for users who write in Hinglish.
  If `XAI_API_KEY` is set, the first *new* conversation after the list is older than `SLANG_REFRESH_HOURS` (default 24) triggers a
  background refresh from Grok. Output is untrusted: parsed, passed through a deterministic filter (format, length, abuse and
  topic blocklist, injection patterns, dedupe, max 12) and swapped in atomically; on any failure the previous list stays. Chat
  never waits for Grok, and the prompt is constant between refreshes so the cache keeps hitting. Every reply is also checked
  against the abuse filter before it is saved.
- **Future work:** graph / mind-map memory for long chats (not needed for short sessions; it would also make the exact token contract harder to keep).

---

Built by **Chahat Saini**.