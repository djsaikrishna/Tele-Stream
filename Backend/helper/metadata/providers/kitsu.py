"""Kitsu anime metadata provider (with ani.zip mappings for IMDb/TMDb/episode art)."""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

import httpx
from rapidfuzz import fuzz

from Backend.helper.metadata.common import KITSU_CACHE, KITSU_THRESHOLD, cached_call, strip_html
from Backend.logger import LOGGER

KITSU_URL = "https://kitsu.io/api/edge"
ANIZIP_URL = "https://api.ani.zip/mappings"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "User-Agent": "Telegram-Stremio (+https://github.com/weebzone/Telegram-Stremio)",
}


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)
        return _client


def _normalize(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        return max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0
    except Exception:
        return 0.0


def _title_score(query: str, attrs: dict) -> float:
    titles = attrs.get("titles") or {}
    candidates = [
        attrs.get("canonicalTitle"),
        titles.get("en"),
        titles.get("en_jp"),
        titles.get("ja_jp"),
        *(attrs.get("abbreviatedTitles") or []),
    ]
    q = _normalize(query)
    best = 0.0
    for cand in candidates:
        cn = _normalize(cand or "")
        if cn:
            best = max(best, _fuzzy(q, cn))
    return best


def _season_queries(title: str, season: Optional[int]) -> List[str]:
    if season and int(season) > 1:
        return [f"{title} Season {season}", f"{title} {season}", title]
    return [title]


async def _kitsu_search(query: str, subtype: Optional[str] = None) -> Optional[dict]:
    try:
        client = await _get_client()
        params = {"filter[text]": query, "page[limit]": 8}
        if subtype:
            params["filter[subtype]"] = subtype
        resp = await client.get(f"{KITSU_URL}/anime", params=params)
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or []
        return data
    except Exception as e:
        LOGGER.warning(f"[KITSU] search failed for '{query}': {e}")
        return None


async def search_anime(title: str, season: Optional[int] = None, movie: bool = False) -> Optional[dict]:
    cache_key = f"kitsu::{'movie' if movie else 'tv'}::{title}::{season}"

    async def _produce():
        best = None
        best_score = 0.0
        subtype = "movie" if movie else None
        for query in _season_queries(title, None if movie else season):
            rows = await _kitsu_search(query, subtype=subtype) or []
            for row in rows:
                attrs = row.get("attributes") or {}
                # Prefer TV for series searches
                if not movie and attrs.get("subtype") in ("movie", "music"):
                    continue
                if movie and attrs.get("subtype") not in ("movie", None, "special", "OVA", "ONA"):
                    # still allow movie subtype primarily
                    if attrs.get("subtype") not in ("movie",):
                        continue
                score = _title_score(title, attrs)
                if score > best_score:
                    best_score = score
                    best = row
            if best_score >= 0.92:
                break

        if best and best_score >= KITSU_THRESHOLD:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] match '{title}' -> '{attrs.get('canonicalTitle')}' "
                f"[{best.get('id')}] score={best_score:.2f}"
            )
            return best
        if best:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] low-confidence for '{title}': "
                f"'{attrs.get('canonicalTitle')}' score={best_score:.2f}"
            )
        return None

    return await cached_call(KITSU_CACHE, cache_key, "kitsu_search", _produce)


async def get_anizip_mappings(kitsu_id: int) -> Optional[dict]:
    cache_key = f"anizip::{kitsu_id}"

    async def _produce():
        try:
            client = await _get_client()
            resp = await client.get(ANIZIP_URL, params={"kitsu_id": kitsu_id})
            return resp.json() if resp.status_code == 200 else None
        except Exception as e:
            LOGGER.warning(f"[KITSU] ani.zip mappings failed for {kitsu_id}: {e}")
            return None

    return await cached_call(KITSU_CACHE, cache_key, "anizip", _produce)


def _anizip_image(images, cover_type: str) -> str:
    for img in images or []:
        if str(img.get("coverType", "")).lower() == cover_type.lower() and img.get("url"):
            return img["url"]
    return ""


def _poster(attrs: dict, images: list) -> str:
    poster = attrs.get("posterImage") or {}
    return (
        poster.get("original")
        or poster.get("large")
        or poster.get("medium")
        or _anizip_image(images, "Poster")
        or ""
    )


def _backdrop(attrs: dict, images: list) -> str:
    cover = attrs.get("coverImage") or {}
    return (
        cover.get("original")
        or cover.get("large")
        or _anizip_image(images, "Fanart")
        or _anizip_image(images, "Banner")
        or ""
    )


def _common_payload(row: dict, doc: dict, title: str) -> dict:
    attrs = row.get("attributes") or {}
    mappings = (doc or {}).get("mappings") or {}
    tmdb_id = mappings.get("themoviedb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (ValueError, TypeError):
        tmdb_id = None

    titles = attrs.get("titles") or {}
    images = (doc or {}).get("images") or []
    rating = attrs.get("averageRating")
    try:
        rate = round(float(rating) / 10.0, 1) if rating else 0
    except (TypeError, ValueError):
        rate = 0
    year = 0
    if attrs.get("startDate"):
        try:
            year = int(str(attrs["startDate"])[:4])
        except (TypeError, ValueError):
            year = 0
    duration = attrs.get("episodeLength")
    return {
        "tmdb_id": tmdb_id,
        "imdb_id": mappings.get("imdb_id"),
        "title": (
            titles.get("en")
            or attrs.get("canonicalTitle")
            or titles.get("en_jp")
            or title
        ),
        "year": year,
        "rate": rate,
        "description": strip_html(attrs.get("synopsis") or attrs.get("description") or ""),
        "poster": _poster(attrs, images),
        "backdrop": _backdrop(attrs, images),
        "logo": _anizip_image(images, "Clearlogo"),
        "genres": [],  # filled below if available
        "cast": [],
        "runtime": f"{duration} min" if duration else "",
        "kitsu_id": row.get("id"),
    }


def _episode_title(ep: dict, season: int, episode: int, absolute: bool = False) -> str:
    ep_title = None
    if isinstance(ep.get("title"), dict):
        ep_title = ep["title"].get("en") or ep["title"].get("ja")
    elif isinstance(ep.get("title"), str):
        ep_title = ep.get("title")
    if ep_title:
        return ep_title
    if absolute or season is None:
        return f"Episode {episode}"
    return f"S{int(season):02d}E{int(episode):02d}"


def _resolve_episode_slot(doc: dict, season, episode, absolute: bool) -> tuple:
    """Map (season, episode) or absolute episode to storage slot + ani.zip ep dict.

    Absolute/orphan style (One Piece 1223):
      - Lookup episodes[str(absolute)]
      - Prefer ani.zip seasonNumber when present
      - Store episode_number as the absolute number so streams stay addressable
        as imdb:season:absolute (common for continuous anime).
    """
    episodes = (doc or {}).get("episodes") or {}
    if absolute or season is None:
        abs_ep = int(episode)
        ep = episodes.get(str(abs_ep)) or {}
        mapped_season = ep.get("seasonNumber") or ep.get("season")
        try:
            mapped_season = int(mapped_season) if mapped_season is not None else 1
        except (TypeError, ValueError):
            mapped_season = 1
        return mapped_season, abs_ep, ep, True

    ep = episodes.get(str(episode)) or {}
    # Also try absolute key if season-relative key missing
    if not ep:
        # search by seasonNumber + relative episode field
        for key, candidate in episodes.items():
            try:
                if int(candidate.get("seasonNumber") or 0) == int(season) and str(candidate.get("episode")) == str(episode):
                    ep = candidate
                    break
            except (TypeError, ValueError):
                continue
    return int(season), int(episode), ep, False


async def fetch_anime_tv(
    title,
    season,
    episode,
    encoded_string,
    year=None,
    quality=None,
    absolute: bool = False,
) -> Optional[dict]:
    # For absolute episodes, search without season suffix (One Piece not "One Piece Season 21")
    search_season = None if absolute or season is None else season
    row = await search_anime(title, season=search_season, movie=False)
    if not row:
        LOGGER.info(f"[KITSU] No match for '{title}' (season={season}, absolute={absolute})")
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    payload = _common_payload(row, doc, title)

    season_number, episode_number, ep, is_abs = _resolve_episode_slot(
        doc, season, episode, absolute or season is None
    )

    if is_abs and not ep:
        LOGGER.info(
            f"[KITSU] Absolute episode {episode} not in ani.zip for '{title}' "
            f"(kitsu={kitsu_id}) — still indexing with season={season_number}"
        )

    payload.update({
        "media_type": "tv",
        "season_number": season_number,
        "episode_number": episode_number,
        "episode_title": _episode_title(ep, season_number, episode_number, absolute=is_abs),
        "episode_backdrop": ep.get("image", "") or "",
        "episode_overview": ep.get("overview") or ep.get("summary") or "",
        "episode_released": ep.get("airDate") or ep.get("airdate") or "",
        "quality": quality,
        "encoded_string": encoded_string,
        "absolute_episode": episode_number if is_abs else None,
    })
    return payload


async def fetch_anime_movie(title, encoded_string, year=None, quality=None) -> Optional[dict]:
    row = await search_anime(title, movie=True)
    if not row:
        # also try without subtype filter
        row = await search_anime(title, movie=False)
        if row:
            subtype = ((row.get("attributes") or {}).get("subtype") or "").lower()
            if subtype not in ("movie", "special", "ova", "ona", ""):
                LOGGER.info(f"[KITSU] No movie match for '{title}'")
                return None
    if not row:
        LOGGER.info(f"[KITSU] No movie match for '{title}'")
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    payload = _common_payload(row, doc, title)
    payload.update({"media_type": "movie", "quality": quality, "encoded_string": encoded_string})
    return payload
