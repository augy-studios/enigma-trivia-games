"""
utils/api.py — Async wrappers around free external REST APIs used by Enigma.
All functions return plain Python dicts/lists; callers handle Discord logic.
"""

import aiohttp
import html
import random
import logging
from config import API

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _session


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()


# ── Open Trivia DB ────────────────────────────────────────────────────────────

async def fetch_trivia(amount: int = 1, category: int | None = None,
                       difficulty: str | None = None) -> list[dict]:
    """
    Fetch trivia questions from Open Trivia DB.
    Returns list of question dicts with keys:
        question, correct_answer, incorrect_answers, category, difficulty, type
    """
    params: dict = {"amount": amount, "encode": "url3986"}
    if category:
        params["category"] = category
    if difficulty:
        params["difficulty"] = difficulty
    try:
        session = await get_session()
        async with session.get(API["trivia"], params=params) as r:
            data = await r.json()
        if data.get("response_code") == 0:
            results = []
            for q in data["results"]:
                results.append({
                    "question":           html.unescape(q["question"]),
                    "correct_answer":     html.unescape(q["correct_answer"]),
                    "incorrect_answers":  [html.unescape(a) for a in q["incorrect_answers"]],
                    "category":           html.unescape(q["category"]),
                    "difficulty":         q["difficulty"],
                    "type":               q["type"],
                })
            return results
    except Exception as e:
        log.error("fetch_trivia error: %s", e)
    return []


async def fetch_trivia_categories() -> list[dict]:
    """Return list of {id, name} trivia category dicts."""
    try:
        session = await get_session()
        async with session.get("https://opentdb.com/api_category.php") as r:
            data = await r.json()
        return data.get("trivia_categories", [])
    except Exception as e:
        log.error("fetch_trivia_categories error: %s", e)
    return []


# ── Quotable (quotes) ─────────────────────────────────────────────────────────

async def fetch_quote() -> dict | None:
    """Return {content, author} or None."""
    try:
        session = await get_session()
        async with session.get(API["quotes"]) as r:
            data = await r.json()
        return {"content": data["content"], "author": data["author"]}
    except Exception as e:
        log.error("fetch_quote error: %s", e)
    return None


# ── Datamuse (synonyms / related words) ───────────────────────────────────────

async def fetch_synonyms(word: str, max_results: int = 20) -> list[str]:
    """Return list of synonym strings for *word*."""
    try:
        session = await get_session()
        params = {"rel_syn": word, "max": max_results}
        async with session.get(API["words"], params=params) as r:
            data = await r.json()
        return [entry["word"] for entry in data]
    except Exception as e:
        log.error("fetch_synonyms error: %s", e)
    return []


async def fetch_word_definition(word: str) -> dict | None:
    """
    Return {word, definition, part_of_speech, origin} or None.
    Uses Free Dictionary API.
    """
    try:
        session = await get_session()
        async with session.get(f"{API['word_def']}{word}") as r:
            if r.status != 200:
                return None
            data = await r.json()
        entry = data[0]
        meaning = entry["meanings"][0]
        defn = meaning["definitions"][0]
        return {
            "word":           entry["word"],
            "part_of_speech": meaning["partOfSpeech"],
            "definition":     defn["definition"],
            "example":        defn.get("example", ""),
            "origin":         entry.get("origin", ""),
            "phonetic":       entry.get("phonetic", ""),
        }
    except Exception as e:
        log.error("fetch_word_definition error: %s", e)
    return None


async def fetch_rhymes(word: str, max_results: int = 10) -> list[str]:
    """Return words that rhyme with *word*."""
    try:
        session = await get_session()
        params = {"rel_rhy": word, "max": max_results}
        async with session.get(API["words"], params=params) as r:
            data = await r.json()
        return [entry["word"] for entry in data]
    except Exception as e:
        log.error("fetch_rhymes error: %s", e)
    return []


# ── Random Word API ───────────────────────────────────────────────────────────

async def fetch_random_word(min_length: int = 4, max_length: int = 12) -> str | None:
    """Return a single random English word."""
    try:
        session = await get_session()
        params = {"number": 10}
        async with session.get(API["word_random"], params=params) as r:
            words = await r.json()
        candidates = [w for w in words if min_length <= len(w) <= max_length]
        return random.choice(candidates) if candidates else (words[0] if words else None)
    except Exception as e:
        log.error("fetch_random_word error: %s", e)
    return None


# ── Useless Facts ─────────────────────────────────────────────────────────────

async def fetch_random_fact() -> str | None:
    """Return a random interesting fact string."""
    try:
        session = await get_session()
        async with session.get(API["facts"]) as r:
            data = await r.json()
        return data.get("text")
    except Exception as e:
        log.error("fetch_random_fact error: %s", e)
    return None


# ── Numbers API (for estimation questions) ────────────────────────────────────

async def fetch_number_fact(number: int | None = None) -> str | None:
    """Return a math or trivia fact about a number."""
    try:
        n = number if number is not None else "random"
        session = await get_session()
        async with session.get(f"{API['numberfacts']}{n}/trivia?json") as r:
            data = await r.json()
        return data.get("text")
    except Exception as e:
        log.error("fetch_number_fact error: %s", e)
    return None
