"""List naming, descriptions, and alphabetical config layout for the mlo-Tek fork."""

from __future__ import annotations

import json
import time
from copy import deepcopy


PROVIDER_LABELS = {
    "amazon-prime": "Amazon Prime",
    "apple-tv": "Apple TV+",
    "crunchyroll": "Crunchyroll",
    "disney": "Disney+",
    "hbo-max": "HBO Max",
    "hulu": "Hulu",
    "joyn": "Joyn",
    "netflix": "Netflix",
    "paramount-plus": "Paramount+",
    "peacock": "Peacock",
    "rtl-plus": "RTL+",
    "wow": "WOW",
}

LOCATION_LABELS = {
    "germany": "Germany",
    "united-states": "United States",
    "world": "Worldwide",
}

TYPE_ORDER = {"movies": 0, "shows": 1, "overall": 2}

# Filled when the config is loaded. Used by the static-list migration code.
LIST_METADATA: dict[str, dict] = {}


def install(sync) -> None:
    _patch_config_layout(sync)
    _patch_static_list_metadata(sync)
    _patch_static_list_sync(sync)
    sync.logger.info(
        "mlo-Tek list layout enabled: alphabetical providers + Today names + descriptions"
    )


def _provider_label(platform: str) -> str:
    platform = (platform or "").strip().lower()
    return PROVIDER_LABELS.get(platform, platform.replace("-", " ").title())


def _location_label(location: str) -> str:
    location = (location or "world").strip().lower()
    return LOCATION_LABELS.get(location, location.replace("-", " ").title())


def _desired_metadata(entry: dict) -> tuple[str, str]:
    provider = _provider_label(entry.get("platform", ""))
    location = _location_label(entry.get("location", "world"))
    media_type = entry.get("type", "both")

    if media_type == "movies":
        return (
            f"Top 10 {provider} Movies Today",
            f"Top 10 {provider} movies in {location}",
        )
    if media_type == "shows":
        return (
            f"Top 10 {provider} TV Shows Today",
            f"Top 10 {provider} TV shows in {location}",
        )
    if media_type == "overall":
        return (
            f"Top 10 {provider} Today",
            f"Top 10 {provider} titles in {location}",
        )
    return (
        f"Top 10 {provider} Today",
        f"Top 10 {provider} titles in {location}",
    )


def _normalize_entries(entries: list[dict]) -> list[dict]:
    normalized = []
    LIST_METADATA.clear()

    for source in entries:
        entry = deepcopy(source)
        old_name = (entry.get("name") or "").strip()
        name, description = _desired_metadata(entry)

        legacy = [
            str(value).strip()
            for value in entry.get("legacyNames", [])
            if str(value).strip()
        ]
        if old_name and old_name.lower() != name.lower() and old_name not in legacy:
            legacy.append(old_name)

        entry["name"] = name
        entry["description"] = description
        entry["normalizeName"] = True
        if legacy:
            entry["legacyNames"] = legacy
        else:
            entry.pop("legacyNames", None)

        LIST_METADATA[name.lower()] = {
            "name": name,
            "description": description,
            "legacy_names": legacy,
        }
        normalized.append(entry)

    normalized.sort(
        key=lambda item: (
            _provider_label(item.get("platform", "")).casefold(),
            TYPE_ORDER.get(item.get("type", "overall"), 99),
            _location_label(item.get("location", "world")).casefold(),
        )
    )
    return normalized


def _patch_config_layout(sync) -> None:
    original_load_config = sync.load_config

    def load_config_with_layout():
        cfg = original_load_config()
        cfg["FlixPatrolTop10"] = _normalize_entries(
            cfg.get("FlixPatrolTop10", [])
        )

        # Also normalize the persistent default.json itself. Read it again from
        # disk so environment-injected API keys are never written back to file.
        try:
            if sync.CONFIG_FILE.exists():
                raw = json.loads(sync.CONFIG_FILE.read_text())
                raw_entries = raw.get("FlixPatrolTop10", [])
                normalized_raw = _normalize_entries(raw_entries)
                if normalized_raw != raw_entries:
                    raw["FlixPatrolTop10"] = normalized_raw
                    sync.CONFIG_FILE.write_text(
                        json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
                    )
                    sync.logger.info(
                        "Normalized config/default.json: providers alphabetized and list metadata updated"
                    )
                # Rebuild runtime metadata from the active config after the raw
                # file pass, because _normalize_entries refreshes LIST_METADATA.
                cfg["FlixPatrolTop10"] = _normalize_entries(
                    cfg.get("FlixPatrolTop10", [])
                )
        except (OSError, ValueError, TypeError) as exc:
            sync.logger.warning("Could not normalize config/default.json: %s", exc)

        return cfg

    sync.load_config = load_config_with_layout


def _candidate_ids(lst: dict) -> list[int]:
    """Return plausible static-list IDs, preferring explicit static ID fields."""
    preferred = (
        "static_list_id",
        "staticlist_id",
        "static_id",
        "staticId",
        "list_id",
        "listid",
        "id",
    )
    ids = []
    for key in preferred:
        value = lst.get(key)
        if value is None:
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def _resolve_static_list_id(sync, mdb, lst: dict):
    """Return only an ID accepted by MDBList's static-list items endpoint.

    `/lists/user` may contain dynamic/linked/etc. records whose generic `id`
    is valid for list metadata but is *not* a static-list ID. `GET /lists/{id}`
    therefore is not a sufficient discriminator. The items endpoint is the
    canonical API used by add/remove operations, so probe that exact endpoint.
    """
    for list_id in _candidate_ids(lst):
        items = mdb.get_list_items(list_id)
        if items is not None:
            return list_id
    return None


def _resolve_created_static_list_id(sync, mdb, result: dict | None, name: str):
    """Resolve a newly created list, tolerating API response-shape differences."""
    if isinstance(result, dict):
        list_id = _resolve_static_list_id(sync, mdb, result)
        if list_id is not None:
            return list_id

    # Creation can become visible fractionally later and some responses do not
    # include the usable static ID. Retry the user's lists briefly.
    for attempt in range(3):
        if attempt:
            time.sleep(0.5)
        for lst in mdb.get_my_lists():
            if str(lst.get("name", "")).lower() != name.lower():
                continue
            list_id = _resolve_static_list_id(sync, mdb, lst)
            if list_id is not None:
                return list_id
    return None


def _patch_static_list_metadata(sync) -> None:
    """Create lists with descriptions and migrate matching legacy list names."""

    def find_or_create_list_with_metadata(mdb, name: str, slug: str):
        metadata = LIST_METADATA.get(name.lower(), {})
        description = metadata.get("description", "")
        legacy_names = {
            value.lower() for value in metadata.get("legacy_names", []) if value
        }

        lists = mdb.get_my_lists()
        for lst in lists:
            list_name = str(lst.get("name", ""))
            is_current = (
                lst.get("slug") == slug
                or list_name.lower() == name.lower()
            )
            is_legacy = list_name.lower() in legacy_names
            if not is_current and not is_legacy:
                continue

            list_id = _resolve_static_list_id(sync, mdb, lst)
            if list_id is None:
                sync.logger.info(
                    "  Ignoring matching non-static list: '%s' (id=%s)",
                    list_name,
                    lst.get("id"),
                )
                continue

            current_description = str(lst.get("description") or "")
            needs_update = (
                list_name != name
                or (description and current_description != description)
            )

            if needs_update:
                if sync.DRY_RUN:
                    sync.logger.info(
                        "  [DRY RUN] Would update list metadata: '%s' -> '%s'",
                        list_name,
                        name,
                    )
                else:
                    payload = {"name": name}
                    if description:
                        payload["description"] = description
                    mdb._req("PUT", f"/lists/{list_id}", json=payload)
                    # Revalidate through the static-list endpoint, not metadata.
                    if mdb.get_list_items(list_id) is not None:
                        sync.logger.info(
                            "  Updated static list metadata: '%s' -> '%s' (static_id=%s)",
                            list_name,
                            name,
                            list_id,
                        )
                    else:
                        sync.logger.warning(
                            "  Static list id=%s failed revalidation; trying another match",
                            list_id,
                        )
                        continue
            else:
                sync.logger.info(
                    "  Static list exists: '%s' (static_id=%s)", list_name, list_id
                )
            return list_id

        if sync.DRY_RUN:
            sync.logger.info("  [DRY RUN] Would create static list '%s'", name)
            if description:
                sync.logger.info("  [DRY RUN] Description: %s", description)
            return None

        sync.logger.info("  Creating static list '%s' ...", name)
        payload = {"name": name}
        if description:
            payload["description"] = description
        result = mdb._req("POST", "/lists/user/add", json=payload)
        list_id = _resolve_created_static_list_id(sync, mdb, result, name)
        if list_id is not None:
            sync.logger.info("  Created static list (static_id=%s)", list_id)
            return list_id

        sync.logger.error("  Failed to create/resolve static list '%s'", name)
        return None

    sync.find_or_create_list = find_or_create_list_with_metadata


def _item_key(item: dict) -> tuple[str, str] | None:
    imdb = item.get("imdb_id") or item.get("imdb")
    if imdb:
        return ("imdb", str(imdb))
    tmdb = item.get("tmdb_id") or item.get("tmdb")
    if tmdb is not None:
        return ("tmdb", str(tmdb))
    return None


def _response_item_keys(existing: dict | None) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not isinstance(existing, dict):
        return keys
    for section in ("movies", "shows"):
        for item in existing.get(section, []) or []:
            key = _item_key(item)
            if key:
                keys.add(key)
    return keys


def _patch_static_list_sync(sync) -> None:
    """Sync only verified static IDs and never log failed API writes as success."""

    def sync_items_verified(mdb, list_id: int, items: list[dict], name: str):
        if not items:
            sync.logger.warning("  No matched items for '%s'", name)
            return False

        if sync.DRY_RUN:
            sync.logger.info(
                "  [DRY RUN] Would sync %d items to '%s'", len(items), name
            )
            return True

        # This exact endpoint must work before any destructive operation.
        existing = mdb.get_list_items(list_id)
        if existing is None:
            sync.logger.error(
                "  Aborting sync for '%s': id=%s is not a usable static-list ID",
                name,
                list_id,
            )
            return False

        movies_add = []
        shows_add = []
        target_keys: set[tuple[str, str]] = set()
        for item in items:
            entry = {}
            if item.get("imdb_id"):
                entry["imdb"] = item["imdb_id"]
            if item.get("tmdb_id"):
                entry["tmdb"] = item["tmdb_id"]
            if not entry:
                continue
            key = _item_key(entry)
            if key:
                target_keys.add(key)
            if item.get("type") == "movie":
                movies_add.append(entry)
            else:
                shows_add.append(entry)

        rm_movies = []
        rm_shows = []
        for movie in existing.get("movies", []) or []:
            entry = {}
            if movie.get("imdb_id"):
                entry["imdb"] = movie["imdb_id"]
            elif movie.get("tmdb_id"):
                entry["tmdb"] = movie["tmdb_id"]
            if entry:
                rm_movies.append(entry)
        for show in existing.get("shows", []) or []:
            entry = {}
            if show.get("imdb_id"):
                entry["imdb"] = show["imdb_id"]
            elif show.get("tmdb_id"):
                entry["tmdb"] = show["tmdb_id"]
            if entry:
                rm_shows.append(entry)

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
            remaining = _response_item_keys(after_remove)
            removed_keys = {
                key
                for entry in [*rm_movies, *rm_shows]
                if (key := _item_key(entry)) is not None
            }
            still_present = removed_keys & remaining
            if still_present:
                sync.logger.error(
                    "  Remove verification failed for '%s' (static_id=%s): %d old item(s) remain",
                    name,
                    list_id,
                    len(still_present),
                )
                return False
            sync.logger.info(
                "  Removed %d old items from '%s' (static_id=%s)",
                len(rm_movies) + len(rm_shows),
                name,
                list_id,
            )

        if not movies_add and not shows_add:
            sync.logger.warning("  No usable IDs to add to '%s'", name)
            return False

        mdb.add_items(list_id, movies_add or None, shows_add or None)
        after_add = mdb.get_list_items(list_id)
        if after_add is None:
            sync.logger.error(
                "  Add failed for '%s' (static_id=%s)", name, list_id
            )
            return False

        present = _response_item_keys(after_add)
        missing = target_keys - present
        if missing:
            sync.logger.error(
                "  Add verification failed for '%s' (static_id=%s): %d/%d target item(s) missing",
                name,
                list_id,
                len(missing),
                len(target_keys),
            )
            return False

        sync.logger.info(
            "  Synced '%s' successfully: %d movies + %d shows (static_id=%s)",
            name,
            len(movies_add),
            len(shows_add),
            list_id,
        )
        return True

    sync.sync_items = sync_items_verified
