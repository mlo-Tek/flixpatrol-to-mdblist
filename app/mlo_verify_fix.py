"""Robust verification for MDBList static-list syncs.

MDBList may remap a submitted TMDB id to another identifier representation
when items are read back. Verification therefore uses both identifier overlap
and final item counts instead of requiring the exact submitted key to reappear.
"""

from __future__ import annotations


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


def _target_payload(items: list[dict]):
    movies_add = []
    shows_add = []
    target_rows = []
    seen = set()

    for item in items:
        entry = {}
        if item.get("imdb_id"):
            entry["imdb"] = item["imdb_id"]
        if item.get("tmdb_id"):
            entry["tmdb"] = item["tmdb_id"]
        if not entry:
            continue

        # Avoid submitting the same media twice when a provider chart contains
        # a duplicate row. Prefer IMDb as the stable dedupe key, otherwise TMDB.
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

        movies_add, shows_add, target_rows = _target_payload(items)
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

        # Exact IDs are ideal, but MDBList can remap a TMDB-only submission to
        # another identifier when returning list contents. If the final list
        # contains the full expected number of unique items, accept it.
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
