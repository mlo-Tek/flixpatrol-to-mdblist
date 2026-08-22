"""mlo-Tek compatibility patches for flixpatrol-to-mdblist.

Keeps the upstream application largely untouched while adding:
- Joyn Germany fallback parsing from FlixPatrol's combined streaming overview.
- Conservative alternate-title matching for subtitle/part-name/number variants.
- MDBList search-query compatibility for punctuation that the API rejects.
- Verified ID fallbacks for exact title/year cases that public metadata can identify.
- A small FileCache race hardening when the cache directory is removed.
"""

from __future__ import annotations

import re
from typing import Optional


PATCH_VERSION = "1.1.0"

# Last-resort, exact title/year/media-type mappings for titles that are known to
# exist but are not discoverable reliably by MDBList/TMDB text search. These are
# intentionally narrow: all three fields must match before an ID is returned.
VERIFIED_IDS = {
    ("the lord of the skies", 2013, "show"): {
        "imdb_id": "tt2777882",
        "tmdb_id": 44953,
    },
    ("fantastic 4 rise of the silver surfer", 2007, "movie"): {
        "imdb_id": "tt0486576",
    },
    ("dschungel divas luxus hat seinen preis", 2026, "show"): {
        "tmdb_id": 329186,
    },
}


def install(sync) -> None:
    """Install runtime patches onto the imported upstream module."""
    _patch_file_cache(sync)
    _patch_mdblist_search_compat(sync)
    _patch_joyn_top10(sync)
    _patch_title_matching(sync)
    sync.logger.info(
        "mlo-Tek patches %s enabled: Joyn fallback + safe matching + query compatibility",
        PATCH_VERSION,
    )


def _patch_file_cache(sync) -> None:
    original_set = sync.FileCache.set

    def set_with_dir_recovery(self, key: str, value):
        if not self.enabled:
            return
        # Handles the harmless race where an admin removes .cache while a sync
        # cycle is still finishing a write.
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return original_set(self, key, value)

    sync.FileCache.set = set_with_dir_recovery


def _patch_mdblist_search_compat(sync) -> None:
    """Keep harmless punctuation from causing MDBList HTTP 400 responses."""
    original_search = sync.MDBListClient.search

    def search_compat(self, title: str, media_type: str, year=None):
        return original_search(
            self,
            _mdblist_safe_query(sync, title),
            media_type,
            year,
        )

    sync.MDBListClient.search = search_compat


def _mdblist_safe_query(sync, title: str) -> str:
    """Normalize only characters known to be problematic for MDBList search."""
    query = sync._clean_search_query(title)
    query = query.translate(str.maketrans({
        "–": "-",
        "—": "-",
        "−": "-",
    }))
    # MDBList currently rejects some searches containing a literal question
    # mark. Removing it does not weaken final matching because TitleMatcher
    # still validates the returned title/year separately.
    query = query.replace("?", "")
    return re.sub(r"\s+", " ", query).strip()


def _patch_joyn_top10(sync) -> None:
    original_get_top10 = sync.FlixPatrolScraper.get_top10

    def get_top10_with_joyn_fallback(
        self,
        platform: str,
        location: str,
        media_type: str = "both",
        limit: int = 10,
        fallback=False,
        kids: bool = False,
    ) -> list[dict]:
        items = original_get_top10(
            self, platform, location, media_type, limit, fallback, kids
        )
        if items or platform.lower() != "joyn":
            return items

        # Joyn's dedicated /top10/joyn/<country> page may not expose the
        # ranking tables even though Joyn is present on the combined streaming
        # overview. Use that canonical overview as a provider-specific fallback.
        sync.logger.info(
            "  Joyn direct page has no usable ranking; trying streaming overview"
        )
        overview_url = f"{sync.FLIXPATROL_BASE}/top10/streaming/{location}/"
        cache_key = f"fp:overview-provider:joyn:{location}"
        cached_html = self.cache.get(cache_key)
        fetched = False

        if cached_html is not None:
            soup = sync.BeautifulSoup(cached_html, "html.parser")
        else:
            soup = self._get(overview_url)
            fetched = soup is not None

        if not soup:
            return []

        sections = _parse_provider_sections(sync, self, soup, "joyn")
        if fetched and sections:
            self.cache.set(cache_key, str(soup))

        results = []
        for mtype in sync._split_types(media_type):
            section_key = (mtype, kids)
            section_items = _dedupe_ranking_items(
                sync, sections.get(section_key, [])
            )[:limit]
            item_type = {
                "movies": "movie",
                "shows": "show",
                "overall": "overall",
            }[mtype]
            for item in section_items:
                item["type"] = item_type
            results.extend(section_items)

        if results:
            sync.logger.info(
                "  Joyn streaming-overview fallback returned %d unique item(s)",
                len(results),
            )
        return results

    sync.FlixPatrolScraper.get_top10 = get_top10_with_joyn_fallback


def _dedupe_ranking_items(sync, items: list[dict]) -> list[dict]:
    """Remove duplicate ranking rows while preserving the first chart position."""
    result = []
    seen = set()
    for item in items:
        title_key = sync.TitleMatcher._norm(item.get("title", ""))
        url = (item.get("url") or "").strip()
        # Within one provider/media chart, identical normalized titles are a
        # duplicate ranking row. Prefer that over URL because FlixPatrol can
        # occasionally emit the same title through two equivalent links.
        key = ("title", title_key) if title_key else ("url", url)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _parse_provider_sections(sync, scraper, soup, provider: str) -> dict:
    """Extract one provider's h3 ranking sections from a combined overview."""
    provider_label = provider.replace("-", " ").strip().lower()
    provider_heading = None

    # The combined page currently uses e.g.:
    #   <h2>Joyn TOP 10 in Germany on August 22, 2026</h2>
    # followed by <h3>TOP 10 Movies</h3> and <h3>TOP 10 TV Shows</h3>.
    for heading in soup.find_all("h2"):
        text = " ".join(heading.stripped_strings).lower()
        if provider_label in text and "top 10" in text:
            provider_heading = heading
            break

    if provider_heading is None:
        sync.logger.warning(
            "  Provider section '%s' not found on streaming overview", provider
        )
        return {}

    sections = {}
    for heading in provider_heading.find_all_next(["h2", "h3"]):
        if heading is provider_heading:
            continue
        if heading.name == "h2":
            break

        text = " ".join(heading.stripped_strings).lower()
        section_key = scraper._classify_heading(text)
        if not section_key:
            continue

        table = scraper._find_next_table(heading)
        if not table:
            continue

        entries = scraper._parse_table(table)
        if entries:
            sections[section_key] = entries
            sync.logger.debug(
                "  %s overview section '%s' -> %d items",
                provider,
                text,
                len(entries),
            )

    return sections


def _patch_title_matching(sync) -> None:
    original_find = sync.TitleMatcher.find

    def find_with_aliases(
        self, title: str, title_info: dict, media_type: str
    ) -> Optional[dict]:
        year = title_info.get("year")
        patch_cache_key = f"match:mlo:v2:{title}:{year}:{media_type}"
        cached = self.cache.get(patch_cache_key)
        if cached is not None:
            return cached if cached != "_MISS_" else None

        best = original_find(self, title, title_info, media_type)
        if best:
            self.cache.set(patch_cache_key, best)
            return best

        aliases = _title_aliases(sync, title)
        for alias in aliases:
            best = _search_exact_alias(sync, self, alias, year, media_type)
            if not best:
                continue

            best["_alias"] = alias
            self.cache.set(patch_cache_key, best)
            sync.logger.info(
                "  Alias match: '%s' -> '%s'", title, alias
            )
            return best

        best = _verified_id_match(sync, title, year, media_type)
        if best:
            self.cache.set(patch_cache_key, best)
            sync.logger.info(
                "  Verified ID match: '%s' (%s) -> %s",
                title,
                year or "?",
                best.get("imdb_id") or f"tmdb:{best.get('tmdb_id')}",
            )
            return best

        self.cache.set(patch_cache_key, "_MISS_")
        return None

    sync.TitleMatcher.find = find_with_aliases


def _title_aliases(sync, title: str) -> list[str]:
    """Generate conservative canonical-title candidates.

    We do not use arbitrary fuzzy matching. Every candidate is a deterministic
    variant of the FlixPatrol title and still has to match a MDBList/TMDB result
    exactly (with the upstream year checks).
    """
    original = sync._clean_search_query(title)
    original_norm = sync.TitleMatcher._norm(original)
    aliases: list[str] = []

    def add(candidate: str) -> None:
        candidate = sync._clean_search_query(candidate)
        candidate = candidate.strip(" -–—:;")
        if not candidate:
            return
        normalized = sync.TitleMatcher._norm(candidate)
        if not normalized or normalized == original_norm:
            return
        if candidate not in aliases:
            aliases.append(candidate)

    # Remove a trailing subtitle introduced with a dash. This is useful for
    # release-specific marketing suffixes such as "... Movie 1 - Akaza's Revenge".
    dash_bases = []
    for separator in (" - ", " – ", " — "):
        if separator in original:
            base = original.split(separator, 1)[0]
            if _specific_enough(sync, base):
                add(base)
                dash_bases.append(base)

    # Strip trailing part markers from both the original and dash-shortened
    # candidates: "Movie 1", "Part 1", "Chapter 1", etc.
    part_pattern = re.compile(
        r"\s+(?:movie|film|part|chapter)\s*(?:[:#-]?\s*)?(?:one|two|three|\d+)\s*$",
        re.IGNORECASE,
    )
    for candidate in [original, *dash_bases]:
        stripped = part_pattern.sub("", candidate).strip()
        if stripped != candidate and _specific_enough(sync, stripped):
            add(stripped)

    # A colon often introduces a regional/marketing subtitle. Only use the
    # prefix when it is long enough to avoid dangerous aliases such as
    # "From", "It", "Us", or "Demon Slayer".
    if ":" in original:
        prefix = original.split(":", 1)[0]
        if _specific_enough(sync, prefix):
            add(prefix)

    # Some services use a digit where the canonical database title spells the
    # number out, e.g. "Fantastic 4" -> "Fantastic Four". Each generated title
    # is still subjected to exact result-title and year validation.
    number_words = {
        "1": "One",
        "2": "Two",
        "3": "Three",
        "4": "Four",
        "5": "Five",
        "6": "Six",
        "7": "Seven",
        "8": "Eight",
        "9": "Nine",
        "10": "Ten",
    }
    for digit, word in number_words.items():
        pattern = re.compile(rf"(?<!\w){re.escape(digit)}(?!\w)")
        if pattern.search(original):
            candidate = pattern.sub(word, original)
            if _specific_enough(sync, candidate):
                add(candidate)

    return aliases


def _specific_enough(sync, title: str) -> bool:
    normalized = sync.TitleMatcher._norm(title)
    words = normalized.split()
    return len(normalized) >= 12 and len(words) >= 3


def _search_exact_alias(sync, matcher, alias: str, year, media_type: str):
    """Search an alias while retaining upstream exact-title/year validation."""
    mtype = (
        "any"
        if media_type == "overall"
        else "movie"
        if media_type == "movie"
        else "show"
    )

    # MDBList first, preserving the upstream source order.
    if year:
        results = matcher.mdb.search(alias, mtype, year)
        best = matcher._pick(results, alias, year)
        if best:
            best["_src"] = "MDBList(alias)"
            return best

    results = matcher.mdb.search(alias, mtype)
    best = matcher._pick(results, alias, year)
    if best:
        best["_src"] = "MDBList(alias)"
        return best

    # Optional TMDB fallback, also requiring an exact match to the alias.
    if not matcher.tmdb or not matcher.tmdb.available:
        return None

    tmdb_types = ("movie", "show") if media_type == "overall" else (mtype,)
    for candidate_type in tmdb_types:
        if year:
            results = matcher.tmdb.search(alias, candidate_type, year)
            best = matcher._pick(results, alias, year)
            if best:
                best["_media_type"] = candidate_type
                best["_src"] = "TMDB(alias)"
                return best

        results = matcher.tmdb.search(alias, candidate_type)
        best = matcher._pick(results, alias, year)
        if best:
            best["_media_type"] = candidate_type
            best["_src"] = "TMDB(alias)"
            return best

    return None


def _verified_id_match(sync, title: str, year, media_type: str) -> Optional[dict]:
    """Return a verified ID only for an exact normalized title/year/type key."""
    if not year or media_type not in ("movie", "show"):
        return None

    key = (sync.TitleMatcher._norm(title), int(year), media_type)
    ids = VERIFIED_IDS.get(key)
    if not ids:
        return None

    result = dict(ids)
    result["year"] = int(year)
    result["_media_type"] = media_type
    result["_src"] = "verified-id"
    return result
