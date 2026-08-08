"""Shared helpers for metadata providers and resolvers."""
from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Optional
from urllib.parse import quote

from rapidfuzz import fuzz

from Backend.logger import LOGGER

# Match thresholds
CINEMETA_THRESHOLD = 0.60
TMDB_THRESHOLD = 0.55
TVDB_THRESHOLD = 0.55
KITSU_THRESHOLD = 0.55
STRONG_MATCH = 0.92
ALT_TITLE_LOOKUPS = 5

# Combined-file constants (Specials season)
COMBINED_SEASON = 0
COMBINED_EPISODE_BASE = 1000

GRADIENT_COVER_BASE = "https://gradient-cover-api.vercel.app"

API_SEMAPHORE = asyncio.Semaphore(12)

# Shared caches (provider modules may also keep their own)
IMDB_CACHE: dict = {}
TMDB_SEARCH_CACHE: dict = {}
TMDB_DETAILS_CACHE: dict = {}
EPISODE_CACHE: dict = {}
ALT_TITLES_CACHE: dict = {}
TVDB_CACHE: dict = {}
KITSU_CACHE: dict = {}

_INFLIGHT: Dict[tuple, asyncio.Future] = {}

_APOSTROPHE_RE = re.compile(r"['\u2018\u2019`\u00B4]")
_SYMBOL_STRIP_RE = re.compile(r"[&.\-:]+")
_HTML_RE = re.compile(r"<[^>]+>")


async def cached_call(store: dict, key, ns: str, producer):
    if key in store:
        return store[key]
    flight_key = (ns, key)
    fut = _INFLIGHT.get(flight_key)
    if fut is not None:
        return await fut
    fut = asyncio.get_running_loop().create_future()
    _INFLIGHT[flight_key] = fut
    try:
        result = await producer()
    except Exception as e:
        _INFLIGHT.pop(flight_key, None)
        if not fut.done():
            fut.set_exception(e)
            fut.exception()
        raise
    store[key] = result
    _INFLIGHT.pop(flight_key, None)
    if not fut.done():
        fut.set_result(result)
    return result


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", _HTML_RE.sub(" ", text)).strip()


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        set_ratio = fuzz.token_set_ratio(a, b) / 100.0
        sort_ratio = fuzz.token_sort_ratio(a, b) / 100.0
        a_tokens, b_tokens = a.split(), b.split()
        coverage = (
            min(len(a_tokens), len(b_tokens)) / max(len(a_tokens), len(b_tokens))
            if a_tokens and b_tokens
            else 0.0
        )
        return max(sort_ratio, set_ratio * coverage)
    except Exception:
        return SequenceMatcher(None, a, b).ratio()


def title_similarity(t1: str, t2: str) -> float:
    n1, n2 = normalize_title(t1), normalize_title(t2)
    return fuzzy_ratio(n1, n2) if n1 and n2 else 0.0


def year_from_str(year_val) -> int:
    if not year_val:
        return 0
    m = re.search(r"(\d{4})", str(year_val))
    return int(m.group(1)) if m else 0


def strip_symbols(text: str) -> str:
    if not text:
        return ""
    text = _APOSTROPHE_RE.sub("", text)
    text = _SYMBOL_STRIP_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_candidate(
    query_title: str,
    query_year: Optional[int],
    result_title: str,
    result_year: int,
    year_reliable: bool = True,
    year_lower_bound: bool = False,
) -> float:
    score = title_similarity(query_title, result_title)
    if score < 0.5:
        return score

    if query_year and result_year:
        if year_lower_bound:
            if int(query_year) >= result_year and score >= 0.80:
                score += 0.15 / (1 + (int(query_year) - result_year) * 0.1)
            return score
        diff = abs(int(query_year) - result_year)
        if year_reliable:
            if diff > 2:
                score = max(0.0, score - 0.10 * (diff - 2))
            elif score >= 0.80:
                if diff == 0:
                    score = min(1.0, score + 0.20)
                elif diff == 1:
                    score = min(1.0, score + 0.07)
        elif diff == 0 and score >= 0.80:
            score = min(1.0, score + 0.05)
    elif query_year and year_reliable and not year_lower_bound:
        score = max(0.0, score - 0.20)
    return score


def build_query_variants(title: str, year: Optional[int] = None) -> list:
    variants = [title]
    if year:
        variants.append(f"{title} {year}")
    stripped = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title)).strip()
    if stripped and stripped.lower() != title.lower():
        variants.append(stripped)
        if year:
            variants.append(f"{stripped} {year}")
    no_article = re.sub(r"^\b(the|a|an)\b\s+", "", title, flags=re.IGNORECASE).strip()
    if no_article and no_article.lower() != title.lower():
        variants.append(no_article)
    seen: set = set()
    ordered = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            ordered.append(v)
    return ordered


def first(value):
    return value[0] if isinstance(value, list) else value


def format_runtime(minutes) -> str:
    return f"{minutes} min" if minutes else ""


def gradient_cover_path(title: str, portrait: bool = False) -> str:
    path = f"/api/image?text={quote((title or 'Media').strip() or 'Media')}&badge="
    return f"{path}&orientation=portrait" if portrait else path


def resolve_cover_url(value: str) -> str:
    value = str(value or "")
    idx = value.find("/api/image?")
    return f"{GRADIENT_COVER_BASE}{value[idx:]}" if idx != -1 else value


def format_tmdb_image(path: str, size="w500") -> str:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ""


def format_imdb_images(imdb_id: str) -> dict:
    if not imdb_id:
        return {"poster": "", "backdrop": "", "logo": ""}
    return {
        "poster": f"https://images.metahub.space/poster/small/{imdb_id}/img",
        "backdrop": f"https://images.metahub.space/background/medium/{imdb_id}/img",
        "logo": f"https://images.metahub.space/logo/medium/{imdb_id}/img",
    }


def extract_default_id(text: str) -> str | None:
    if not text:
        return None
    bare_imdb = re.search(r"\b(tt\d{7,10})\b", text)
    if bare_imdb:
        return bare_imdb.group(1)
    imdb_url = re.search(r"/title/(tt\d+)", text)
    if imdb_url:
        return imdb_url.group(1)
    tmdb_url = re.search(r"/(?:movie|tv)/(\d+)", text)
    if tmdb_url:
        return tmdb_url.group(1)
    return None


def split_default_id(default_id) -> tuple:
    """Returns (imdb_id, tmdb_id, explicit_imdb, use_tmdb)."""
    if not default_id:
        return None, None, False, False
    value = str(default_id).strip()
    if value.startswith("tt"):
        return value, None, True, False
    if value.isdigit():
        return None, int(value), False, True
    return None, None, False, False


def empty_payload_base() -> dict:
    return {
        "tmdb_id": None,
        "imdb_id": None,
        "title": "",
        "year": 0,
        "rate": 0,
        "description": "",
        "poster": "",
        "backdrop": "",
        "logo": "",
        "cast": [],
        "runtime": "",
        "genres": [],
        "original_language": None,
        "origin_country": [],
    }
