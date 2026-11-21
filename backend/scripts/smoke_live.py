"""Run ONCE before deploying to prove your real Gemini keys + model + cache work.

    cd backend
    LLM_API_KEYS=key1,key2 python scripts/smoke_live.py
    (optional) MODEL=gemini/gemini-2.5-flash to try another model

Expected: key_1 and key_2 both answer; the 3rd call is a cache hit (even with the other key).
"""
import asyncio
import os
import sys
import time

import litellm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import llm  # noqa: E402

MODEL = os.getenv("MODEL", "gemini/gemini-2.0-flash")
KEYS = [k.strip() for k in os.getenv("LLM_API_KEYS", "").split(",") if k.strip()]
MSGS = [{"role": "user", "content": "Say hi in 5 words."}]


async def call(tag, key, caching):
    t = time.perf_counter()
    try:
        r = await litellm.acompletion(model=MODEL, messages=MSGS, api_key=key, caching=caching, num_retries=0, timeout=30)
    except Exception as e:
        print(f"{tag}: FAILED -> {type(e).__name__}: {str(e)[:160]}")
        return
    hit = (r._hidden_params or {}).get("cache_hit")
    print(f"{tag}: ok | cache_hit={hit} | {int((time.perf_counter()-t)*1000)} ms | "
          f"in={r.usage.prompt_tokens} out={r.usage.completion_tokens} | {r.choices[0].message.content.strip()[:60]!r}")


async def main():
    if len(KEYS) < 2:
        sys.exit("Set LLM_API_KEYS=key1,key2")
    llm.setup_cache()
    print("model:", MODEL)
    await call("key_1 live ", KEYS[0], True)
    await asyncio.sleep(0.5)  # litellm writes the cache in the background
    await call("key_2 (hit?)", KEYS[1], True)
    await call("key_1 (hit?)", KEYS[0], True)


asyncio.run(main())
