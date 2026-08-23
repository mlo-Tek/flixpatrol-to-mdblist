"""Robust verification for MDBList static-list syncs.

MDBList may remap a submitted TMDB id to another identifier representation
when items are read back. Verification therefore uses both identifier overlap
and final item counts instead of requiring the exact submitted key to reappear.

For TMDB-only matches we also try to resolve an IMDb id through TMDB's
external_ids endpoint before writing to MDBList. MDBList's static-list API is
more reliable with IMDb identifiers for a few newly added/re-cut titles.
"""

from __future__ import annotations

import os


# Exact, publicly verified aliases for cases where the streaming chart exposes a
# newly named cut/edition but it is still the same IMDb feature film.
VERIFIED_IMDB_WRITE_IDS = {
    ("the x-files: i want to believe – vrach frankenshteyn", 2026, "movie"): "tt0443701",
    ("the x-files: i want to believe - vrach frankenshteyn", 2026, "movie"): "tt0443701",
}


def _ids(item: dict) -> set[tuple[str, str]]:
    ids: set[tuple[str, str]] = set()
    imdb = item.get("imdb_id") or item.get("imdb")
    tmdb = item.get("tmdb_id") or item.get("tmdb")
    if imdb:
        ids.add(("imdb", str(imdb)))
    if tmdb is not None:
        ids.add(("tmdb", str(tmdb)))
    return ids


def _flatten(existing: dict | None) -> list[dict]:
    if not isinstance(existing, dict):
        return []
    return [*(existing.get("movies", []) or []), *(existing.get("shows", []) or [])]


def _count(existing: dict | None) -> int:
    return len(_flatten(existing))


def _verified_imdb_id(item: dict):
    title = str(item.get("title") or "").strip().lower()
    try:
        year = int(item.get("year")) if item.get("year") is not None else None
    except (TypeError, ValueError):
        year = None
    media_type = str(item.get("type") or "").strip().lower()
    return VERIFIED_IMDB_WRITE_IDS.get((title, year, media_type))


def _tmdb_external_imdb(sync, item: dict):
    """Resolve IMDb from TMDB external_ids for a TMDB-only matched item."""
    tmdb_id = item.get("tmdb_id") or item.get("tmdb")
    if not tmdb_id or item.get("imdb_id") or item.get("imdb"):
        return None

    api_key = os.environ.get("TMDB_API_KEY", "").strip()
    if not api_key:
        return None

    media_type = str(item.get("type") or "").lower()
    endpoint_type = "movie" if media_type == "movie" else "tv" if media_type == "show" else None
    if endpoint_type is None:
        return None

    try:
        response = sync.requests.get(
            f"{sync.TMDB_API_BASE}/{endpoint_type}/{tmdb_id}/external_ids",
            params={"api_key": api_key},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (sync.requests.RequestException, ValueError, TypeError) as exc:
        sync.logger.warning(
            "  Could not resolve TMDB external IDs for '%s' (tmdb=%s): %s",
            item.get("title") or "?",
            tmdb_id,
            exc,
        )
        return None

    imdb_id = data.get("imdb_id") if isinstance(data, dict) else None
    if imdb_id:
        sync.logger.info(
            "  Enriched TMDB-only match with IMDb: '%s' | tmdb=%s -> imdb=%s",
            item.get("title") or "?",
            tmdb_id,
            imdb_id,
        )
        return str(imdb_id)
    return None


def _preferred_imdb_id(sync, item: dict):
    existing = item.get("imdb_id") or item.get("imdb")
    if existing:
        return str(existing)

    verified = _verified_imdb_id(item)
    if verified:
        sync.logger.info(
            "  Using verified IMDb write ID for '%s': %s",
            item.get("title") or "?",
            verified,
        )
        return verified

    return _tmdb_external_imdb(sync, item)


def _target_payload(sync, items: list[dict]):
    movies_add = []
    shows_add = []
    target_rows = []
    seen = set()

    for item in items:
        entry = {}
        imdb_id = _preferred_imdb_id(sync, item)
        if imdb_id:
            # Prefer IMDb alone when available. This avoids an occasional MDBList
            # rejection where a new TMDB id is not yet mapped by the static API.
            entry["imdb"] = imdb_id
        elif item.get("tmdb_id"):
            entry["tmdb"] = item["tmdb_id"]
        if not entry:
            continue

        key = (
            ("imdb", str(entry["imdb"]))
            if entry.get("imdb")
            else ("tmdb", str(entry["tmdb"]))
        )
        if key in seen:
            continue
        seen.add(key)

        row = {
            "title": item.get("title") or "?",
            "year": item.get("year"),
            "type": item.get("type") or "?",
            "imdb": entry.get("imdb"),
            "tmdb": entry.get("tmdb"),
            "ids": _ids(entry),
        }
        target_rows.append(row)

        if item.get("type") == "movie":
            movies_add.append(entry)
        else:
            shows_add.append(entry)

    return movies_add, shows_add, target_rows


def _remove_payload(existing: dict | None):
    movies = []
    shows = []
    if not isinstance(existing, dict):
        return movies, shows

    for source, target in ((existing.get("movies", []) or [], movies), (existing.get("shows", []) or [], shows)):
        for item in source:
            entry = {}
            if item.get("imdb_id") or item.get("imdb"):
                entry["imdb"] = item.get("imdb_id") or item.get("imdb")
            if item.get("tmdb_id") or item.get("tmdb"):
                entry["tmdb"] = item.get("tmdb_id") or item.get("tmdb")
            if entry:
                target.append(entry)
    return movies, shows


def _unmatched_targets(target_rows: list[dict], existing: dict | None) -> list[dict]:
    returned_ids = set()
    for item in _flatten(existing):
        returned_ids |= _ids(item)

    return [row for row in target_rows if row["ids"] and row["ids"].isdisjoint(returned_ids)]


def install(sync) -> None:
    """Install count-aware static-list write verification."""

    def sync_items_count_aware(mdb, list_id: int, items: list[dict], name: str):
        if not items:
            sync.logger.warning("  No matched items for '%s'", name)
            return False

        if sync.DRY_RUN:
            sync.logger.info("  [DRY RUN] Would sync %d items to '%s'", len(items), name)
            return True

        existing = mdb.get_list_items(list_id)
        if existing is None:
            sync.logger.error(
                "  Aborting sync for '%s': static_id=%s is not readable",
                name,
                list_id,
            )
            return False

        movies_add, shows_add, target_rows = _target_payload(sync, items)
        target_count = len(target_rows)
        if not target_count:
            sync.logger.warning("  No usable unique IDs to add to '%s'", name)
            return False

        rm_movies, rm_shows = _remove_payload(existing)
        before_count = _count(existing)
        if rm_movies or rm_shows:
            mdb.remove_items(list_id, rm_movies or None, rm_shows or None)
            after_remove = mdb.get_list_items(list_id)
            if after_remove is None:
                sync.logger.error(
                    "  Remove failed for '%s' (static_id=%s); aborting add",
                    name,
                    list_id,
                )
                return False
            after_remove_count = _count(after_remove)
            if after_remove_count != 0:
                sync.logger.error(
                    "  Remove verification failed for '%s' (static_id=%s): %d old item(s) remain",
                    name,
                    list_id,
                    after_remove_count,
                )
                return False
            sync.logger.info(
                "  Removed %d old items from '%s' (static_id=%s)",
                before_count,
                name,
                list_id,
            )

        mdb.add_items(list_id, movies_add or None, shows_add or None)
        after_add = mdb.get_list_items(list_id)
        if after_add is None:
            sync.logger.error("  Add failed for '%s' (static_id=%s)", name, list_id)
            return False

        final_count = _count(after_add)
        unmatched = _unmatched_targets(target_rows, after_add)

        if final_count == target_count:
            if unmatched:
                sync.logger.info(
                    "  Synced '%s' successfully by count: %d/%d items present; %d item(s) were returned with remapped IDs (static_id=%s)",
                    name,
                    final_count,
                    target_count,
                    len(unmatched),
                    list_id,
                )
            else:
                sync.logger.info(
                    "  Synced '%s' successfully: %d movies + %d shows (static_id=%s)",
                    name,
                    len(movies_add),
                    len(shows_add),
                    list_id,
                )
            return True

        if final_count < target_count:
            shortfall = target_count - final_count
            sync.logger.error(
                "  Add verification failed for '%s' (static_id=%s): final count %d/%d, shortfall=%d",
                name,
                list_id,
                final_count,
                target_count,
                shortfall,
            )
            for row in unmatched[:max(shortfall, 1)]:
                sync.logger.error(
                    "    Candidate missing item: '%s' | type=%s | year=%s | imdb=%s | tmdb=%s",
                    row["title"],
                    row["type"],
                    row["year"] or "?",
                    row["imdb"] or "-",
                    row["tmdb"] or "-",
                )
            if len(unmatched) > shortfall:
                sync.logger.info(
                    "  Note: %d additional target(s) did not match returned IDs, likely because MDBList remapped identifiers",
                    len(unmatched) - shortfall,
                )
            return False

        sync.logger.warning(
            "  Sync verification anomaly for '%s' (static_id=%s): final count %d exceeds expected %d",
            name,
            list_id,
            final_count,
            target_count,
        )
        return False

    sync.sync_items = sync_items_count_aware
    sync.logger.info("mlo-Tek count-aware static-list verification enabled")
