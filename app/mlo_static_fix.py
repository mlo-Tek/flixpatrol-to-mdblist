"""Fix MDBList static-list ID handling and improve sync diagnostics.

MDBList exposes a generic list metadata id and, for static lists, a separate
static-list id used by the /items/add and /items/remove endpoints. Reading
/list/{id}/items is not sufficient to distinguish them because non-static
lists can also be readable there. Keep the two identifiers separate.
"""

from __future__ import annotations

import time

import mlo_list_layout as layout


STATIC_ID_KEYS = (
    "static_list_id",
    "staticlist_id",
    "static_id",
    "staticId",
)


def _as_positive_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def explicit_static_id(record: dict | None):
    """Return only an explicitly identified MDBList static-list id."""
    if not isinstance(record, dict):
        return None
    for key in STATIC_ID_KEYS:
        value = _as_positive_int(record.get(key))
        if value is not None:
            return value
    return None


def metadata_id(record: dict | None):
    """Return the generic list id used for metadata operations."""
    if not isinstance(record, dict):
        return None
    return _as_positive_int(record.get("id"))


def _matching_record(records, name: str, slug: str, legacy_names: set[str]):
    for record in records or []:
        list_name = str(record.get("name", ""))
        if (
            record.get("slug") == slug
            or list_name.lower() == name.lower()
            or list_name.lower() in legacy_names
        ):
            yield record


def _resolve_created_static_id(mdb, result: dict | None, name: str):
    """Resolve the write id after POST /lists/user/add.

    Prefer an explicit static id from either the create response or the next
    /lists/user snapshots. Only if MDBList returns no explicit field at all do
    we trust the create response's id, because that endpoint creates a static
    list by definition.
    """
    value = explicit_static_id(result)
    if value is not None:
        return value

    for attempt in range(5):
        if attempt:
            time.sleep(0.5)
        for record in mdb.get_my_lists():
            if str(record.get("name", "")).lower() != name.lower():
                continue
            value = explicit_static_id(record)
            if value is not None:
                return value

    # The create endpoint itself is specifically "Create Static List".
    return metadata_id(result)


def _identifier_set(items_response: dict | None) -> set[tuple[str, str]]:
    """Collect every IMDb/TMDB identifier returned by a static-list read."""
    identifiers: set[tuple[str, str]] = set()
    if not isinstance(items_response, dict):
        return identifiers

    for section in ("movies", "shows"):
        for item in items_response.get(section, []) or []:
            imdb = item.get("imdb_id") or item.get("imdb")
            tmdb = item.get("tmdb_id") or item.get("tmdb")
            if imdb:
                identifiers.add(("imdb", str(imdb)))
            if tmdb is not None:
                identifiers.add(("tmdb", str(tmdb)))
    return identifiers


def _item_identifiers(item: dict) -> set[tuple[str, str]]:
    identifiers: set[tuple[str, str]] = set()
    imdb = item.get("imdb_id") or item.get("imdb")
    tmdb = item.get("tmdb_id") or item.get("tmdb")
    if imdb:
        identifiers.add(("imdb", str(imdb)))
    if tmdb is not None:
        identifiers.add(("tmdb", str(tmdb)))
    return identifiers


def _patch_missing_item_diagnostics(sync) -> None:
    """Log title and IDs for items MDBList did not retain after an add call."""
    original_sync_items = sync.sync_items

    def sync_items_with_diagnostics(mdb, list_id: int, items: list[dict], name: str):
        original_get_list_items = mdb.get_list_items
        original_add_items = mdb.add_items
        reads = []
        add_called = False

        def recording_get_list_items(requested_list_id):
            response = original_get_list_items(requested_list_id)
            reads.append(response)
            return response

        def recording_add_items(requested_list_id, movies=None, shows=None):
            nonlocal add_called
            add_called = True
            return original_add_items(requested_list_id, movies, shows)

        mdb.get_list_items = recording_get_list_items
        mdb.add_items = recording_add_items
        try:
            result = original_sync_items(mdb, list_id, items, name)
        finally:
            mdb.get_list_items = original_get_list_items
            mdb.add_items = original_add_items

        # Only diagnose a failed sync after an actual add attempt. This avoids
        # reporting every target as missing when the failure happened earlier,
        # for example while clearing old items.
        if result is False and add_called and reads:
            present_ids = _identifier_set(reads[-1])
            missing_items = []
            for item in items:
                wanted_ids = _item_identifiers(item)
                if wanted_ids and wanted_ids.isdisjoint(present_ids):
                    missing_items.append(item)

            if missing_items:
                sync.logger.error(
                    "  MDBList rejected/missed %d item(s) for '%s' (static_id=%s):",
                    len(missing_items),
                    name,
                    list_id,
                )
                for item in missing_items:
                    sync.logger.error(
                        "    Missing item: '%s' | type=%s | year=%s | imdb=%s | tmdb=%s",
                        item.get("title") or "<unknown>",
                        item.get("type") or "?",
                        item.get("year") or "?",
                        item.get("imdb_id") or "-",
                        item.get("tmdb_id") or "-",
                    )

        return result

    sync.sync_items = sync_items_with_diagnostics


def install(sync) -> None:
    """Override broken static-list lookup/create path and add diagnostics."""

    def find_or_create_static_list(mdb, name: str, slug: str):
        metadata = layout.LIST_METADATA.get(name.lower(), {})
        description = metadata.get("description", "")
        legacy_names = {
            str(value).lower()
            for value in metadata.get("legacy_names", [])
            if value
        }

        records = mdb.get_my_lists()
        for record in _matching_record(records, name, slug, legacy_names):
            list_name = str(record.get("name", ""))
            static_id = explicit_static_id(record)
            generic_id = metadata_id(record)

            if static_id is None:
                sync.logger.info(
                    "  Ignoring matching non-static list: '%s' (metadata_id=%s)",
                    list_name,
                    generic_id,
                )
                continue

            needs_update = (
                list_name != name
                or (
                    description
                    and str(record.get("description") or "") != description
                )
            )

            if needs_update and not sync.DRY_RUN:
                # Metadata and static-item operations use different ids.
                update_id = generic_id or static_id
                payload = {"name": name}
                if description:
                    payload["description"] = description
                mdb._req("PUT", f"/lists/{update_id}", json=payload)
                sync.logger.info(
                    "  Updated list metadata: '%s' -> '%s' "
                    "(metadata_id=%s, static_id=%s)",
                    list_name,
                    name,
                    update_id,
                    static_id,
                )
            elif needs_update:
                sync.logger.info(
                    "  [DRY RUN] Would update list metadata: '%s' -> '%s'",
                    list_name,
                    name,
                )
            else:
                sync.logger.info(
                    "  Static list exists: '%s' (metadata_id=%s, static_id=%s)",
                    list_name,
                    generic_id,
                    static_id,
                )

            return static_id

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
        static_id = _resolve_created_static_id(mdb, result, name)
        if static_id is None:
            sync.logger.error("  Failed to create/resolve static list '%s'", name)
            return None

        sync.logger.info(
            "  Created static list '%s' (static_id=%s)", name, static_id
        )
        return static_id

    sync.find_or_create_list = find_or_create_static_list
    _patch_missing_item_diagnostics(sync)
    sync.logger.info(
        "mlo-Tek static-list ID fix enabled: metadata_id/static_id separated + missing-item diagnostics"
    )
