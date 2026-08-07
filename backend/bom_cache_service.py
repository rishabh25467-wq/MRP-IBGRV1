"""Persistent, change-aware BOM node cache backed by MongoDB.

Lets the Purchasing Plan feature reuse previously-exploded BOM structure
across runs instead of re-exploding live from SAP every single time (which
can take many minutes on a large plan - see purchasing_plan.py). Two
responsibilities, cleanly split:

  - `build_tree_from_cache()`: called at Purchasing Plan generation time.
    Reads each node's raw BOM data from the local cache (near-instant, no
    network calls) and assembles the same hierarchical tree shape
    `sap_soap_client.explode_bom()` produces. Any product_id never seen
    before is lazily fetched live from SAP (one-time cost) and persisted for
    next time - it is NOT written to the cache if the live fetch itself
    fails (so a transient SAP hiccup doesn't get "remembered" as a false
    "no BOM" and just gets retried on the next run).

  - `refresh_stale_nodes()`: run periodically by a background task (see
    server.py's startup scheduler). For every product_id already in the
    cache, does ONE lightweight SAP fetch (not a full recursive explosion)
    and compares the returned BOM's revision suffix against what's stored -
    only writes an update if the revision actually changed, so most cycles
    are cheap no-ops rather than full re-explosions. A failed fetch here
    leaves the existing cached entry untouched (same "don't erase good data
    on a transient error" principle).
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_DEPTH = 6
MAX_LOOKUPS = 300
COLLECTION_NAME = "bom_node_cache"
FETCH_RETRY_ATTEMPTS = 3


OVERRIDES_COLLECTION_NAME = "bom_variant_overrides"


class BomFetchError(Exception):
    """A genuine SAP/network error (timeout, HTTP failure, etc), even after
    retries - distinct from SAP cleanly confirming a product has no BOM.
    Raised (not swallowed) for the ROOT product a caller asks to explode, so
    purchasing_plan.py can report an accurate "SAP lookup failed, unresolved"
    reason instead of misleadingly claiming "SAP confirmed no BOM" (see bug
    report: a persistent-vs-transient distinction the UI now surfaces).
    Sub-node lookups deeper in a tree stay tolerant of this (a timeout on one
    sub-assembly must not blow up resolving the rest of the tree)."""


def _fetch_live(sap_soap_client, product_id, db=None):
    """One lightweight (non-recursive) SAP fetch for a single product's own
    BOM header+items - NOT a full explosion. Returns the raw bom dict, or
    None if SAP cleanly confirms there is no BOM for this product. Retries
    up to FETCH_RETRY_ATTEMPTS times with backoff before raising
    BomFetchError - this tenant frequently hits connect-timeouts under the
    concurrent load a Purchasing Plan run generates, and a single-shot
    failure was previously indistinguishable from "SAP confirms no BOM",
    causing materials that genuinely have a BOM in SAP to be misreported as
    missing (see bug report: dozens of real parts falsely flagged "No SAP
    BOM found" that resolved fine on a plain retry).

    If `db` is given and the fetched BOM has genuine alternates (see
    sap_soap_client._parse - multiple currently-active revisions using
    DIFFERENT materials for the same output, not just an admin revision
    chain), checks `bom_variant_overrides` for a production-made choice and
    re-fetches that EXACT revision by ID instead of the default (highest
    revision) - see production_plan_service.py, where production resolves
    which alternate to use for a given component."""
    last_error = None
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        try:
            raw = sap_soap_client._fetch_bom_by_id(product_id) or sap_soap_client._fetch_bom_by_output_product(product_id)
            break
        except Exception as e:
            last_error = e
            if attempt < FETCH_RETRY_ATTEMPTS - 1:
                logger.warning(f"BOM fetch attempt {attempt + 1}/{FETCH_RETRY_ATTEMPTS} failed for '{product_id}', retrying: {e}")
                time.sleep(1.5 * (attempt + 1))
    else:
        raise BomFetchError(str(last_error)) from last_error

    if db is not None and raw and raw.get("alternates"):
        override = db[OVERRIDES_COLLECTION_NAME].find_one({"_id": product_id})
        if override and override["chosen_bom_id"] != raw["bom_id"]:
            try:
                chosen = sap_soap_client._fetch_bom_by_id(override["chosen_bom_id"])
            except Exception as e:
                logger.warning(f"BOM variant override lookup failed for '{product_id}' -> '{override['chosen_bom_id']}', using default instead: {e}")
                chosen = None
            if chosen:
                chosen["alternates"] = raw["alternates"]
                raw = chosen
    return raw


def _upsert(collection, product_id, raw_bom, changed):
    now = datetime.now(timezone.utc)
    update = {
        "bom_id": raw_bom["bom_id"] if raw_bom else None,
        "groups": raw_bom["groups"] if raw_bom else [],
        "product_uuid": raw_bom.get("product_uuid") if raw_bom else None,
        "alternates": raw_bom.get("alternates", []) if raw_bom else [],
        "found": bool(raw_bom and raw_bom.get("groups")),
        "last_checked_at": now,
    }
    if changed:
        update["last_changed_at"] = now
    collection.update_one({"_id": product_id}, {"$set": update}, upsert=True)


def build_tree_from_cache(root_id: str, sap_soap_client, db):
    """Cache-first equivalent of sap_soap_client.explode_bom() - same output
    shape ({bom_id, total_components, max_level, tree}), plus a
    `min_checked_at` timestamp (the oldest last_checked_at among every node
    actually used, so callers can show how fresh the underlying BOM data
    is), sourced from the local Mongo cache wherever possible, falling back
    to a live SAP fetch (and caching the result) only for product_ids never
    seen before. Raises BomFetchError if the ROOT itself can't be reached
    (distinct from a clean "return None" when SAP confirms no BOM exists)."""
    collection = db[COLLECTION_NAME]
    min_checked_at = None

    def track(checked_at):
        nonlocal min_checked_at
        if checked_at and (min_checked_at is None or checked_at < min_checked_at):
            min_checked_at = checked_at

    def get_cached(product_id):
        """Returns (raw_bom_or_None, was_already_cached, checked_at)."""
        doc = collection.find_one({"_id": product_id})
        if doc is None:
            return None, False, None
        raw = {"bom_id": doc.get("bom_id"), "groups": doc.get("groups", [])} if doc.get("found") else None
        return raw, True, doc.get("last_checked_at")

    def get_or_fetch(product_id):
        """Tolerant fetch used for BFS sub-nodes deeper in the tree - a
        failure here just means that one sub-assembly's own children aren't
        expanded this run, it must never abort resolving the rest of the
        tree (see BomFetchError docstring)."""
        raw, was_cached, checked_at = get_cached(product_id)
        if was_cached:
            track(checked_at)
            return raw
        try:
            raw = _fetch_live(sap_soap_client, product_id, db)
        except BomFetchError as e:
            logger.warning(f"BOM fetch failed for '{product_id}' (leaving uncached for retry next run): {e}")
            return None
        now = datetime.now(timezone.utc)
        _upsert(collection, product_id, raw, changed=True)
        track(now)
        return raw

    # Root fetch: unlike sub-nodes, a failure here must propagate as an
    # error (not be swallowed into "no BOM") - see BomFetchError.
    raw, was_cached, checked_at = get_cached(root_id)
    if was_cached:
        track(checked_at)
        root = raw
    else:
        root = _fetch_live(sap_soap_client, root_id, db)
        now = datetime.now(timezone.utc)
        _upsert(collection, root_id, root, changed=True)
        track(now)

    if root is None:
        return None

    total_components = 0
    max_level_seen = 0
    lookups_done = 0
    root_children = []
    frontier = [(root, 1, frozenset({root_id, root["bom_id"]}), root_children, 1.0)]

    while frontier and lookups_done < MAX_LOOKUPS:
        candidate_ids = set()
        for bom, level, ancestors, _, _ in frontier:
            if level >= MAX_DEPTH:
                continue
            for group in bom["groups"]:
                for item in group["items"]:
                    pid = item["product_id"]
                    if item["active"] and pid not in ancestors:
                        candidate_ids.add(pid)

        to_resolve = list(candidate_ids)[: max(0, MAX_LOOKUPS - lookups_done)]
        resolved = {}
        uncached_ids = []
        for pid in to_resolve:
            raw, was_cached, checked_at = get_cached(pid)
            if was_cached:
                track(checked_at)
                resolved[pid] = raw
            else:
                uncached_ids.append(pid)

        if uncached_ids:
            now = datetime.now(timezone.utc)

            def fetch_and_cache(pid):
                try:
                    raw = _fetch_live(sap_soap_client, pid, db)
                except BomFetchError as e:
                    logger.warning(f"BOM fetch failed for '{pid}' (leaving uncached for retry next run): {e}")
                    return None, False
                _upsert(collection, pid, raw, changed=True)
                return raw, True

            with ThreadPoolExecutor(max_workers=8) as executor:
                fetched = list(executor.map(fetch_and_cache, uncached_ids))
            for pid, (raw, was_persisted) in zip(uncached_ids, fetched):
                resolved[pid] = raw
                if was_persisted:
                    track(now)

        lookups_done += len(to_resolve)

        next_frontier = []
        for bom, level, ancestors, children_out, parent_cum_qty in frontier:
            max_level_seen = max(max_level_seen, level)
            for group in bom["groups"]:
                for item in group["items"]:
                    if not item["active"]:
                        continue
                    cum_qty = round(item["quantity"] * parent_cum_qty, 6) if item["quantity"] is not None else None
                    node = {
                        "level": level,
                        "group_id": group["group_id"],
                        "item_id": item["item_id"],
                        "product_id": item["product_id"],
                        "product_uuid": item["product_uuid"],
                        "description": item["description"],
                        "quantity": cum_qty,
                        "unit_of_measure": item["unit_of_measure"],
                        "eco_id": item["eco_id"],
                        "active": item["active"],
                        "has_sub_bom": False,
                        "children": [],
                    }
                    children_out.append(node)
                    total_components += 1

                    sub_bom = resolved.get(item["product_id"])
                    if sub_bom and sub_bom["groups"] and item["product_id"] not in ancestors:
                        node["has_sub_bom"] = True
                        next_frontier.append((
                            sub_bom, level + 1, ancestors | {item["product_id"]},
                            node["children"], cum_qty if cum_qty is not None else parent_cum_qty,
                        ))

        frontier = next_frontier

    return {
        "bom_id": root["bom_id"],
        "total_components": total_components,
        "max_level": max_level_seen,
        "tree": root_children,
        "min_checked_at": min_checked_at,
    }


def refresh_stale_nodes(sap_soap_client, db, max_workers: int = 5) -> dict:
    """Background maintenance: for every product_id already in the cache, do
    ONE lightweight SAP fetch and update only if its BOM revision actually
    changed. A failed fetch leaves the existing entry untouched (retried next
    cycle). Returns {"checked": n, "changed": n, "failed": n}."""
    collection = db[COLLECTION_NAME]
    product_ids = [doc["_id"] for doc in collection.find({}, {"_id": 1})]
    stats = {"checked": 0, "changed": 0, "failed": 0}
    if not product_ids:
        return stats

    def check_one(product_id):
        existing = collection.find_one({"_id": product_id})
        try:
            raw = _fetch_live(sap_soap_client, product_id, db)
        except BomFetchError as e:
            logger.warning(f"Background refresh: fetch failed for '{product_id}', keeping existing cache entry: {e}")
            return "failed"
        old_bom_id = existing.get("bom_id") if existing else None
        new_bom_id = raw["bom_id"] if raw else None
        changed = old_bom_id != new_bom_id
        _upsert(collection, product_id, raw, changed=changed)
        return "changed" if changed else "unchanged"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(check_one, product_ids))

    for outcome in results:
        if outcome == "failed":
            stats["failed"] += 1
        else:
            stats["checked"] += 1
            if outcome == "changed":
                stats["changed"] += 1
    return stats
