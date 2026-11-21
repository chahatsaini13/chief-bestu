"""LiteLLM gateway: key pool + automatic failover + built-in cache.

Division of labour:
  * LiteLLM  -> talks to the model AND owns the response cache (litellm.cache).
  * This loop -> owns per-key failover, because we need to report exactly which
                 key answered (key_id) and how many keys failed (fallback_attempts).
"""
import logging
import time

import litellm
from litellm.caching.caching import Cache

log = logging.getLogger("chief-bestu")

SIMULATABLE_STATUSES = {429, 500, 502, 503}


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed."""


def setup_cache() -> None:
    # In-memory cache. Cache key = model + messages (+ sampling params); the
    # api_key is NOT part of it, so a reply produced by key_1 is a hit for key_2.
    litellm.cache = Cache()
    litellm.drop_params = True


def parse_simulation(header_value, enabled: bool) -> dict:
    """'key_1=429,key_2=500' or '*=429' -> {'key_1': 429, ...}. Ignored unless enabled."""
    if not enabled or not header_value:
        return {}
    out = {}
    for part in header_value.split(","):
        if "=" not in part:
            continue
        key, _, status = part.partition("=")
        key, status = key.strip(), status.strip()
        if status.isdigit() and int(status) in SIMULATABLE_STATUSES and key:
            out[key] = int(status)
    return out


def _simulated_exception(status: int, model: str) -> Exception:
    """Builds the SAME exception class LiteLLM raises for a real provider error."""
    provider = "gemini"
    msg = f"simulated provider failure ({status})"
    if status == 429:
        return litellm.RateLimitError(message=msg, llm_provider=provider, model=model)
    if status == 502:
        return litellm.BadGatewayError(message=msg, llm_provider=provider, model=model)
    if status == 503:
        return litellm.ServiceUnavailableError(message=msg, llm_provider=provider, model=model)
    return litellm.InternalServerError(message=msg, llm_provider=provider, model=model)


def provider_extra_kwargs(api_key: str) -> dict:
    """Hook for tests (they inject mock_response here). Empty in production."""
    return {}


async def _call_key(model, messages, api_key, key_id, simulation, use_cache, timeout):
    """One attempt with one key. A simulated failure raises the same exception a
    real provider failure would, so it travels through the exact same failover path."""
    status = simulation.get(key_id) or simulation.get("*")
    if status:
        raise _simulated_exception(status, model)
    return await litellm.acompletion(
        model=model,
        messages=messages,
        api_key=api_key,
        caching=use_cache,
        timeout=timeout,
        num_retries=0,  # we do our own key-level failover
        **provider_extra_kwargs(api_key),
    )


async def chat_with_failover(model, messages, api_keys, simulation, timeout=30.0):
    """Returns (reply_text, metadata_without_latency). Raises AllProvidersUnavailable."""
    # If a failure is being simulated, skip the cache READ (caching=False skips the
    # read; LiteLLM still writes the fresh answer) so failover can be demonstrated
    # even for a message that is already cached.
    sim_active = any(k == "*" or k in {f"key_{i + 1}" for i in range(len(api_keys))} for k in simulation)
    use_cache = not sim_active

    failures = 0
    for idx, api_key in enumerate(api_keys):
        key_id = f"key_{idx + 1}"
        try:
            resp = await _call_key(model, messages, api_key, key_id, simulation, use_cache, timeout)
            reply = (resp.choices[0].message.content or "").strip()
            if not reply:
                raise ValueError("empty reply from provider")  # treat as a failed key
        except Exception as exc:  # any provider/network failure -> try next key
            failures += 1
            # never log the key itself, only its id and the error class
            log.warning("%s failed (%s); trying next key", key_id, type(exc).__name__)
            continue

        hidden = getattr(resp, "_hidden_params", None) or {}
        hit = bool(hidden.get("cache_hit"))
        usage = getattr(resp, "usage", None)
        meta = {
            "resolution_source": "LLM_CACHE" if hit else "LIVE_INFERENCE",
            "model": model,
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "key_id": None if hit else key_id,
            "fallback_attempts": 0 if hit else failures,
        }
        return reply, meta

    raise AllProvidersUnavailable()


def now_ms() -> float:
    return time.perf_counter() * 1000
