"""List naming, descriptions, and alphabetical config layout for the mlo-Tek fork."""

from __future__ import annotations

import json
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

            list_id = lst["id"]
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
                    result = mdb._req(
                        "PUT", f"/lists/{list_id}", json=payload
                    )
                    if result is not None:
                        sync.logger.info(
                            "  Updated list metadata: '%s' -> '%s'",
                            list_name,
                            name,
                        )
                    else:
                        sync.logger.warning(
                            "  Could not update metadata for list id=%s; continuing with existing list",
                            list_id,
                        )
            else:
                sync.logger.info(
                    "  List exists: '%s' (id=%s)", list_name, list_id
                )
            return list_id

        if sync.DRY_RUN:
            sync.logger.info("  [DRY RUN] Would create list '%s'", name)
            if description:
                sync.logger.info("  [DRY RUN] Description: %s", description)
            return None

        sync.logger.info("  Creating list '%s' ...", name)
        payload = {"name": name}
        if description:
            payload["description"] = description
        result = mdb._req("POST", "/lists/user/add", json=payload)
        if result and "id" in result:
            sync.logger.info("  Created (id=%s)", result["id"])
            return result["id"]

        # Some API responses do not return the new ID immediately.
        for lst in mdb.get_my_lists():
            if str(lst.get("name", "")).lower() == name.lower():
                return lst["id"]

        sync.logger.error("  Failed to create list '%s'", name)
        return None

    sync.find_or_create_list = find_or_create_list_with_metadata
