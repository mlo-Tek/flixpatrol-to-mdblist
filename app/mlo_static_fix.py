"""Fix MDBList static-list reuse and improve sync diagnostics.

MDBList's /lists/user records use the normal `id` for list operations and mark
dynamic lists with `dynamic=true`. Earlier fork patches incorrectly required a
separate `static_list_id` field, so existing static lists were ignored and a
new list was created on every scheduled run. Reuse existing non-dynamic list
IDs and only create a list when no matching list exists at all.
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
    """Return a separately named static-list id when an API variant provides one."""
    if not isinstance(record, dict):
        return None
    for key in STATIC_ID_KEYS:
        value = _as_positive_int(record.get(key))
        if value is not None:
            return value
    return None


def metadata_id(record: dict | None):
    """Return MDBList's normal list id."""
    if not isinstance(record, dict):
        return None
    return _as_positive_int(record.get("id"))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def static_write_id(record: dict | None):
    """Return the ID that may be used for static-list item writes.

    MDBList's /lists/user response exposes static lists as normal list records.
    Their plain `id` is the ID used by /lists/{id}/items/add|remove. Dynamic
    lists are explicitly marked with `dynamic=true` and must not be used for
    static-list writes.

    Some API variants may expose an explicit static id; prefer that when it is
    present. If `dynamic` is omitted, fail closed against duplicate creation by
    reusing the matching normal ID rather than creating another same-named list.
    A bad/unsupported ID then causes the content sync to fail visibly, but no
    duplicate list is created.
    """
    if not isinstance(record, dict):
        return None

    explicit = explicit_static_id(record)
    if explicit is not None:
        return explicit

    generic = metadata_id(record)
    if generic is None:
        return None

    list_type = str(
        record.get("list_type") or record.get("type") or ""
    ).strip().lower()
    if list_type in {"dynamic", "generated"}:
        return None
    if list_type == "static" or record.get("static") is True:
        return generic

    if "dynamic" in record and _truthy(record.get("dynamic")):
        return None

    # `/lists/user` uses the regular id for static lists. Missing/false dynamic
    # therefore means the record is reusable for our static list.
    return generic


def _matching_record(records, name: str, slug: str, legacy_names: set[str]):
    for record in records or []:
        list_name = str(record.get("name", ""))
        if (
            record.get("slug") == slug
            or list_name.lower() == name.lower()
            or list_name.lower() in legacy_names
        ):
            yield record


def _match_priority(record: dict, name: str, slug: str):
    """Prefer exact current names, then exact slug, then the newest list id."""
    list_name = str(record.get("name", ""))
    exact_name = list_name.lower() == name.lower()
    exact_slug = record.get("slug") == slug
    write_id = static_write_id(record) or 0
    return (1 if exact_name else 0, 1 if exact_slug else 0, write_id)


def _resolve_created_static_id(mdb, result: dict | None, name: str):
    """Resolve the write id after POST /lists/user/add.

    `/lists/user/add` creates a static list, so its returned normal `id` is safe
    to use. If the response lacks an ID, briefly retry /lists/user and select a
    reusable same-name record.
    """
    value = explicit_static_id(result) or metadata_id(result)
    if value is not None:
        return value

    for attempt in range(5):
        if attempt:
            time.sleep(0.5)
        candidates = []
        for record in mdb.get_my_lists():
            if str(record.get("name", "")).lower() != name.lower():
                continue
            value = static_write_id(record)
            if value is not None:
                candidates.append((value, record))
        if candidates:
            return max(candidates, key=lambda pair: pair[0])[0]
    return None


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
    """Reuse existing static lists and create only when no matching list exists."""

    def find_or_create_static_list(mdb, name: str, slug: str):
        metadata = layout.LIST_METADATA.get(name.lower(), {})
        description = metadata.get("description", "")
        legacy_names = {
            str(value).lower()
            for value in metadata.get("legacy_names", [])
            if value
        }

        records = mdb.get_my_lists()
        matches = list(_matching_record(records, name, slug, legacy_names))
        reusable = [record for record in matches if static_write_id(record) is not None]

        if reusable:
            # Bad earlier versions may already have created duplicates. Reuse a
            # single deterministic list from now on and never create another.
            record = max(reusable, key=lambda item: _match_priority(item, name, slug))
            list_name = str(record.get("name", ""))
            static_id = static_write_id(record)
            generic_id = metadata_id(record) or static_id

            if len(reusable) > 1:
                sync.logger.warning(
                    "  Found %d matching static lists for '%s'; reusing static_id=%s and creating no new list",
                    len(reusable),
                    name,
                    static_id,
                )

            needs_update = (
                list_name != name
                or (
                    description
                    and str(record.get("description") or "") != description
                )
            )

            if needs_update and not sync.DRY_RUN:
                payload = {"name": name}
                if description:
                    payload["description"] = description
                mdb._req("PUT", f"/lists/{generic_id}", json=payload)
                sync.logger.info(
                    "  Reusing existing static list after metadata update: '%s' -> '%s' (id=%s)",
                    list_name,
                    name,
                    static_id,
                )
            elif needs_update:
                sync.logger.info(
                    "  [DRY RUN] Would update metadata on existing static list: '%s' -> '%s' (id=%s)",
                    list_name,
                    name,
                    static_id,
                )
            else:
                sync.logger.info(
                    "  Reusing existing static list: '%s' (id=%s)",
                    list_name,
                    static_id,
                )

            return static_id

        # A same-name/slug record exists but is explicitly dynamic or otherwise
        # unusable. Do not create another same-named list. It is safer to fail
        # this list's sync than to grow duplicates on every schedule run.
        if matches:
            sync.logger.error(
                "  Matching MDBList list already exists for '%s' but is not usable as a static list; refusing to create a duplicate",
                name,
            )
            return None

        if sync.DRY_RUN:
            sync.logger.info("  [DRY RUN] Would create missing static list '%s'", name)
            if description:
                sync.logger.info("  [DRY RUN] Description: %s", description)
            return None

        # Creation is allowed only when no current-name, slug, or legacy-name
        # record exists in /lists/user.
        sync.logger.info("  No matching list exists; creating static list '%s' ...", name)
        payload = {"name": name}
        if description:
            payload["description"] = description
        result = mdb._req("POST", "/lists/user/add", json=payload)
        static_id = _resolve_created_static_id(mdb, result, name)
        if static_id is None:
            sync.logger.error("  Failed to create/resolve static list '%s'", name)
            return None

        sync.logger.info(
            "  Created missing static list '%s' (id=%s)", name, static_id
        )
        return static_id

    sync.find_or_create_list = find_or_create_static_list
    _patch_missing_item_diagnostics(sync)
    sync.logger.info(
        "mlo-Tek static-list reuse fix enabled: existing non-dynamic list IDs are reused; duplicate creation blocked"
    )
