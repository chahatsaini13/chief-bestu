"""Slang menu for the Chief Bestu persona.

How it works (no human in the loop):
  * A seed list ships in slang.txt. It is always available as a fallback.
  * If XAI_API_KEY is set, the first NEW conversation after the list goes stale
    (default 24h, SLANG_REFRESH_HOURS) triggers a BACKGROUND refresh from Grok.
    The chat request never waits for Grok: it uses the current list, and the next
    conversation sees the refreshed one.
  * Grok output is untrusted. It goes through a deterministic filter (format, length,
    blocklist, prompt-injection patterns, dedupe, max entries). If too little survives,
    or Grok errors/times out, the previous list stays in place.
  * The rendered prompt text is a constant until the list actually changes, so the
    LiteLLM cache keeps hitting between refreshes.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path

import litellm

log = logging.getLogger("chief-bestu")

SEED_PATH = Path(__file__).with_name("slang.txt")
MAX_ENTRIES = 12
MIN_ENTRIES = 5  # a refresh that yields fewer than this is rejected as bad output
PHRASE_MAX = 30
FIELD_MAX = 120
RETRY_AFTER_FAILURE_S = 900
GROK_TIMEOUT_S = 60

# Abuse / gaali detection. Works on a normalised form so spelling tricks still match:
# leetspeak (m@darchod), masking (chu*tiya), repeated letters (chuuutiya), spaced-out letters (b c, b.c).
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
# Distinctive stems: matched anywhere inside a word.
_ABUSE_STEMS = (
    "chutiy", "chutia", "chootiy", "madarch", "maderch", "madrch", "behench", "bhench", "behnch",
    "bhosd", "bhosr", "bhonsd", "lauda", "lavda", "lawda", "harami", "kamina", "kamine",
    "fuck", "shit", "bitch", "slut", "whore", "cunt", "pussy", "asshole", "bastard", "nigga", "nigger",
    "motherfu", "dumbass", "jackass",
)
# Short or ambiguous words (they appear inside innocent words, e.g. "explode", "grandiose"):
# only match when they are the WHOLE token.
_ABUSE_TOKENS = {
    "bc", "mc", "mkc", "bkl", "bsdk", "bsdke", "bhk", "tmkc", "mf", "mfer", "wtf", "stfu", "gtfo", "gandu",
    "chu", "chut", "lund", "lode", "loda", "gand", "gaand", "randi", "dick", "tatti",
    "fck", "fuk", "fuq", "fcuk", "btch", "mdrchod", "bhnchod", "kutta", "kutti", "kutte", "saala", "saali",
    "suar", "bakchod", "retard", "retarded",
}
# Input is normalised with repeated letters collapsed, so collapse the word lists the same way
# (otherwise "tatti", "kutta" or "asshole" could never match).
_collapse = lambda w: re.sub(r"(.)\1+", r"\1", w)  # noqa: E731
_ABUSE_STEMS = tuple(_collapse(w) for w in _ABUSE_STEMS)
_ABUSE_TOKENS = {_collapse(w) for w in _ABUSE_TOKENS}
_DATE_ERA = re.compile(r"\b\d[\d,.]*\s*(?:bc|bce|ad)\b", re.I)  # "300 BC" is history, not abuse
# Other topics a friendly slang menu should stay away from.
_BLOCKED_WORDS = [
    r"sex\w*", r"porn\w*", r"nsfw", r"nude\w*", r"rape\w*", r"suicid\w*", r"kill\w*", r"murder\w*", r"drug\w*",
    r"weed", r"ganja", r"bjp", r"congress", r"modi", r"rahul", r"trump", r"politic\w*", r"election\w*", r"vote\w*",
    r"hindu\w*", r"muslim\w*", r"islam\w*", r"christian\w*", r"sikh\w*", r"dalit\w*", r"caste\w*", r"jihad\w*",
    r"kafir\w*",
]
_BLOCKED = re.compile(r"\b(?:" + "|".join(_BLOCKED_WORDS) + r")\b", re.I)


def _normalise_tokens(text: str) -> list:
    tokens = []
    for raw in (text or "").lower().split():
        t = re.sub(r"[^a-z0-9@$!]", "", raw.translate(_LEET))  # strip masks like * . - _
        t = re.sub(r"[^a-z]", "", t)
        t = re.sub(r"(.)\1+", r"\1", t)  # chuuutiya -> chutiya
        if t:
            tokens.append(t)
    # join runs of single letters: "b c" / "b . c" -> "bc"
    joined, run = [], ""
    for t in tokens:
        if len(t) == 1:
            run += t
            continue
        if run:
            joined.append(run)
            run = ""
        joined.append(t)
    if run:
        joined.append(run)
    return joined


def contains_abuse(text: str) -> bool:
    text = _DATE_ERA.sub(" ", text or "")
    for tok in _normalise_tokens(text):
        if tok in _ABUSE_TOKENS or any(stem in tok for stem in _ABUSE_STEMS):
            return True
    return False


_INJECTION = re.compile(r"(ignore|instruction|system prompt|prompt|http|www\.|@|[<>{}`])", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def parse_entries(raw: str) -> list:
    """'phrase | meaning | use when | never when' lines -> list of 4-tuples. Skips junk lines."""
    out = []
    for line in (raw or "").splitlines():
        line = _BULLET.sub("", line.strip().strip("`"))
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4 or parts[0].lower() == "phrase":
            continue
        out.append(tuple(parts))
    return out


def filter_entries(entries: list) -> list:
    """Deterministic safety/format filter. Order is preserved, duplicates dropped, capped."""
    seen, kept = set(), []
    for entry in entries:
        phrase, meaning, use_when, never_when = entry
        if not all((phrase, meaning, use_when, never_when)):
            continue
        if not (2 <= len(phrase) <= PHRASE_MAX) or len(phrase.split()) > 5:
            continue
        if any(not (3 <= len(f) <= FIELD_MAX) for f in (meaning, use_when, never_when)):
            continue
        text = " ".join(entry)
        if contains_abuse(text) or _BLOCKED.search(text) or _INJECTION.search(text):
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(entry)
        if len(kept) >= MAX_ENTRIES:
            break
    return kept


def render_block(entries: list) -> str:
    lines = [
        "SLANG MENU (optional seasoning)",
        "Pick AT MOST ONE phrase from this menu, and only if it fits the user's mood right now. "
        "If nothing fits, use none.",
        'Never use a phrase in its "never when" situation. Never use any slang when the user is sad, '
        "scared or in real trouble.",
    ]
    for phrase, meaning, use_when, never_when in entries:
        lines.append(f'- "{phrase}" = {meaning}. Use when: {use_when}. Never when: {never_when}.')
    return "\n".join(lines)


def _grok_prompt() -> str:
    return (
        f"Today is {date.today().isoformat()}. List up to 15 Hindi/Hinglish internet slang phrases that are "
        "trending right now among young Indians on X, Instagram and YouTube.\n"
        "Rules: family-friendly, no abuse, nothing about politics, religion, caste, sex or violence, no real "
        "people's names, each phrase at most 5 words.\n"
        "Output ONLY lines in exactly this format, no intro, no numbering, no extra text:\n"
        "phrase | short meaning | when a friendly CEO-type bestfriend should use it | when it must NEVER be used"
    )


def make_grok_fetch(api_key: str, model: str):
    """Returns an async fn(prompt)->str, or None when no key is configured."""
    if not api_key:
        return None

    async def fetch(prompt: str) -> str:
        # caching=False: never serve yesterday's answer from the in-process LiteLLM cache.
        resp = await litellm.acompletion(
            model=model,
            api_key=api_key,
            messages=[{"role": "user", "content": prompt}],
            timeout=45,
            num_retries=0,
            caching=False,
        )
        return resp.choices[0].message.content or ""

    return fetch


class SlangStore:
    def __init__(self, base_prompt: str, seed_path=SEED_PATH, live_path=None, refresh_hours: float = 24.0, fetch=None):
        self.base_prompt = base_prompt
        self.seed_path = Path(seed_path)
        self.live_path = Path(live_path) if live_path else None
        self.refresh_hours = refresh_hours
        self.fetch = fetch
        self._task = None
        self._next_try = 0.0
        self._entries, self._fetched_at = self._load()
        self._prompt = self._build()

    # ----- public -----
    @property
    def prompt(self) -> str:
        """Full default system prompt. Identical string until the list changes."""
        return self._prompt

    @property
    def entries(self) -> list:
        return list(self._entries)

    def is_stale(self) -> bool:
        if self.fetch is None or time.time() < self._next_try:
            return False
        return self.refresh_hours <= 0 or time.time() - self._fetched_at >= self.refresh_hours * 3600

    def ensure_fresh(self) -> None:
        """Called when a new conversation starts. Never blocks and never raises."""
        if self.is_stale() and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.refresh())

    async def refresh(self) -> bool:
        try:
            raw = await asyncio.wait_for(self.fetch(_grok_prompt()), timeout=GROK_TIMEOUT_S)
            entries = filter_entries(parse_entries(raw))
            if len(entries) < MIN_ENTRIES:
                raise ValueError(f"only {len(entries)} usable entries")
        except Exception as exc:  # Grok down, bad key, junk output: keep the last good list
            log.warning("slang refresh failed (%s); keeping previous list", type(exc).__name__)
            self._next_try = time.time() + RETRY_AFTER_FAILURE_S
            return False
        self._entries, self._fetched_at, self._next_try = entries, time.time(), 0.0
        self._prompt = self._build()
        self._save()
        log.info("slang list refreshed (%d entries)", len(entries))
        return True

    # ----- internals -----
    def _build(self) -> str:
        return self.base_prompt.rstrip() + "\n\n" + render_block(self._entries) + "\n"

    def _load(self):
        if self.live_path and self.live_path.exists():
            try:
                data = json.loads(self.live_path.read_text(encoding="utf-8"))
                entries = filter_entries([tuple(e) for e in data["entries"] if len(e) == 4])
                if len(entries) >= MIN_ENTRIES:
                    return entries, float(data["fetched_at"])
            except Exception as exc:
                log.warning("ignoring unreadable live slang file (%s)", type(exc).__name__)
        entries = filter_entries(parse_entries(self.seed_path.read_text(encoding="utf-8")))
        return entries, 0.0

    def _save(self) -> None:
        if not self.live_path:
            return
        try:
            self.live_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.live_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": self._fetched_at, "entries": self._entries}), encoding="utf-8")
            os.replace(tmp, self.live_path)  # atomic: readers never see a half-written file
        except OSError as exc:
            log.warning("could not persist slang list (%s)", type(exc).__name__)