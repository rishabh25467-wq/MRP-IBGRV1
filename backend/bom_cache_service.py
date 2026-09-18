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

from sap_soap_client import BATCH_CHUNK_SIZE

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


def _apply_override(sap_soap_client, db, product_id, raw):
    """If `raw` has genuine alternates (see sap_soap_client._parse -
    multiple currently-active revisions using DIFFERENT materials for the
    same output, not just an admin revision chain) and production has saved
    a standing choice for this product_id that differs from the default-
    picked revision (bom_variant_overrides), re-fetches and substitutes that
    EXACT chosen revision instead - see production_plan_service.py. Shared
    by both the single-product (_fetch_live) and batched (_fetch_live_batch)
    fetch paths - override lookups are rare enough (a manual, per-product
    production decision) that batching them isn't worth the complexity;
    they're applied as a small per-product post-processing step regardless
    of which path produced `raw`."""
    if not (db is not None and raw and raw.get("alternates")):
        return raw
    override = db[OVERRIDES_COLLECTION_NAME].find_one({"_id": product_id})
    if not override or override["chosen_bom_id"] == raw["bom_id"]:
        return raw
    try:
        chosen = sap_soap_client._fetch_bom_by_id(override["chosen_bom_id"])
    except Exception as e:
        logger.warning(f"BOM variant override lookup failed for '{product_id}' -> '{override['chosen_bom_id']}', using default instead: {e}")
        return raw
    if chosen:
        chosen["alternates"] = raw["alternates"]
        return chosen
    return raw


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

    See _apply_override for the `db`-driven BOM-alternate substitution."""
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

    return _apply_override(sap_soap_client, db, product_id, raw)


def _fetch_live_batch(sap_soap_client, product_ids: list, db=None) -> dict:
    """Batched analogue of _fetch_live() for MANY never-before-cached
    product_ids at once (Feb 2026 speedup - same technique already applied
    to BOM Explorer's explode_bom(), extended here to Purchasing Plan/MRP's
    cache-miss and background-refresh paths). Chunks product_ids into
    BATCH_CHUNK_SIZE-sized groups and does ONE SOAP round-trip per chunk
    (via sap_soap_client._safe_fetch_boms_batch, already retried 2x
    internally) instead of one call per product - cuts wall-clock cost of
    warming up a large batch of never-seen products roughly by a factor of
    BATCH_CHUNK_SIZE, with NO increase in concurrent SAP load (same
    sap_rate_limiter.py cap either way - fewer, bigger round-trips, not more
    of them).

    Note: unlike _fetch_live, this only searches by OUTPUT product (SAP's
    SelectionByOutputProductID) - it skips the SelectionByProductionBillOf
    MaterialID direct-ID-match _fetch_live tries first. That first attempt
    is a no-op for the overwhelming majority of callers here (OMS part
    numbers / FG item codes are bare product codes, not literal revisioned
    BOM IDs like "P26663_1") - correctness is preserved either way since any
    id this misses simply won't be pre-warmed and falls through to the
    normal, unabridged _fetch_live path the first time it's actually needed
    (see build_tree_from_cache/bulk_prefetch below), it just loses the
    speedup for that one rare id.

    Returns {product_id: bom_dict_or_None} for every id that got a genuine
    response (bom_dict is None when SAP cleanly confirms no BOM); a
    product_id absent from the result means its WHOLE chunk failed after
    retries (a real connectivity issue, not "confirmed no BOM") - callers
    leave it unresolved/uncached for the next run, same as a BomFetchError."""
    if not product_ids:
        return {}
    chunks = [product_ids[i:i + BATCH_CHUNK_SIZE] for i in range(0, len(product_ids), BATCH_CHUNK_SIZE)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        chunk_results = list(executor.map(sap_soap_client._safe_fetch_boms_batch, chunks))
    results = {}
    for chunk_result in chunk_results:
        results.update(chunk_result)
    for pid, raw in list(results.items()):
        results[pid] = _apply_override(sap_soap_client, db, pid, raw)
    return results


def bulk_prefetch(product_ids: list, sap_soap_client, db) -> dict:
    """Pre-warms the persistent cache for MANY product_ids in one batched
    pass - intended to be called ONCE upfront with every root candidate a
    Purchasing Plan/MRP run is about to explode (see purchasing_plan.py's
    _resolve_boms and mrp_service.py's build_mrp_plan), instead of letting
    each one trigger its own individual live SAP call as build_tree_from_
    cache's root-fetch would otherwise do, sequentially, one part at a
    time. Skips ids already cached (idempotent/cheap to call on every run -
    a warm cache costs nothing here, just one Mongo $in query). Silently
    leaves any id whose batch fetch failed uncached - it will raise
    BomFetchError as usual (and be reported as a "fetch_error" in the
    caller's missing/unresolved list) the first time build_tree_from_cache
    actually reaches it, same as if bulk_prefetch had never run.

    Also used (Aug 2026) by the "Full Sync" job's catalog-wide prefetch
    step against EVERY inventory item - see inventory_service._compute_
    no_bom_flags: `deep_expand_all_known_roots` alone only ever re-walks
    roots ALREADY in this cache, it can't discover a genuine BOM root that
    was NEVER individually looked up by any page (BOM Explorer/Purchasing
    Plan/MRP/L1L2 Report). This is exactly the gap that made a freshly-
    deployed environment's "No BOM" count much higher than one with a lot
    of page-usage history behind it, even after re-running the nightly
    sync. Returns {"checked", "newly_fetched", "found"} for visibility."""
    if not product_ids:
        return {"checked": 0, "newly_fetched": 0, "found": 0}
    collection = db[COLLECTION_NAME]
    already_cached = {doc["_id"] for doc in collection.find({"_id": {"$in": list(product_ids)}}, {"_id": 1})}
    to_fetch = [pid for pid in product_ids if pid not in already_cached]
    if not to_fetch:
        return {"checked": len(product_ids), "newly_fetched": 0, "found": 0}
    fetched = _fetch_live_batch(sap_soap_client, to_fetch, db)
    found = 0
    for pid in to_fetch:
        if pid in fetched:
            _upsert(collection, pid, fetched[pid], changed=True)
            if fetched[pid]:
                found += 1
    return {"checked": len(product_ids), "newly_fetched": len(to_fetch), "found": found}


def _upsert(collection, product_id, raw_bom, changed):
    now = datetime.now(timezone.utc)
    update = {
        "bom_id": raw_bom["bom_id"] if raw_bom else None,
        "groups": raw_bom["groups"] if raw_bom else [],
        "product_uuid": raw_bom.get("product_uuid") if raw_bom else None,
        "alternates": raw_bom.get("alternates", []) if raw_bom else [],
        # Superseded/old-ECO input product IDs for this parent - see
        # sap_soap_client._build_bom_from_hit_blocks. Purely informational
        # (see inventory_service._compute_no_bom_flags): the active "No
        # BOM" flag logic never reads this, it only powers a separate
        # "found in historical BOM" transparency note on the Inventory page.
        "historical_input_ids": raw_bom.get("historical_input_ids", []) if raw_bom else [],
        # Components of OTHER Consistent alternate revisions of this same
        # parent (see sap_soap_client._build_bom_from_hit_blocks) - these
        # ARE currently active/usable, just not part of the single
        # canonical `groups` tree kept for cost rollup/explosion. Read by
        # inventory_service._compute_no_bom_flags alongside `groups`'s own
        # leaves so an alternate-only component never gets falsely flagged.
        "active_alternate_ids": raw_bom.get("active_alternate_ids", []) if raw_bom else [],
        "found": bool(raw_bom and raw_bom.get("groups")),
        "last_checked_at": now,
    }
    if changed:
        update["last_changed_at"] = now
    collection.update_one({"_id": product_id}, {"$set": update}, upsert=True)


def persist_live_fetch(db, product_id: str, raw_bom: dict) -> None:
    """Sep 18 2026 fix - real incident: production_confirmation_service's
    "Check Live Stock" live BOM re-fetch (see its _resolve_bom_doc) used
    to only correct that ONE response, never the stored `bom_node_cache`
    doc itself - so a component removed from a BOM in SAP kept showing
    up in the DEFAULT (cache) view of the BOM Component Stock panel even
    after a live check had already proven it gone, until this collection's
    own scheduled refresh cycle happened to reach that product. Any
    caller that does a one-off live SAP re-fetch of a BOM should call
    this immediately after, so the cache self-heals right away instead of
    waiting on the schedule."""
    _upsert(db[COLLECTION_NAME], product_id, raw_bom, changed=True)


def is_cached(root_id: str, db) -> bool:
    """Sep 8 2026 (BOM Explorer speed fix): true if `root_id` has EVER been
    resolved before (found or confirmed-no-BOM, doesn't matter which) -
    lets a caller decide "serve from cache, near-instant" vs. "this is a
    brand-new part, needs an actual live SAP explosion" BEFORE paying for
    any fetch at all."""
    return db[COLLECTION_NAME].find_one({"_id": root_id}, {"_id": 1}) is not None


def build_tree_from_cache(root_id: str, sap_soap_client, db, force_refresh: bool = False, progress_cb=None):
    """Cache-first equivalent of sap_soap_client.explode_bom() - same output
    shape ({bom_id, total_components, max_level, tree}), plus a
    `min_checked_at` timestamp (the oldest last_checked_at among every node
    actually used, so callers can show how fresh the underlying BOM data
    is), sourced from the local Mongo cache wherever possible, falling back
    to a live SAP fetch (and caching the result) only for product_ids never
    seen before. Raises BomFetchError if the ROOT itself can't be reached
    (distinct from a clean "return None" when SAP confirms no BOM exists).

    `force_refresh=True` (Sep 8 2026, BOM Explorer's "Refresh from SAP"
    button) makes every node in this exploration act as if uncached - a
    full live re-walk of the whole tree, same as the old always-live
    behavior, EXCEPT every freshly-fetched node still gets `_upsert`ed back
    into the cache as it goes, so a subsequent non-refresh search benefits
    from this refresh too.

    `progress_cb(level, lookups_done, max_lookups)`, if given, is called
    once per BFS level of live SAP lookups actually performed (never
    called for a level entirely served from cache) - lets a caller (a
    background job, see server.py's /bom/search/live) report incremental
    progress on what would otherwise be a single opaque multi-second-to-
    40s wait."""
    collection = db[COLLECTION_NAME]
    min_checked_at = None

    def track(checked_at):
        nonlocal min_checked_at
        if checked_at and (min_checked_at is None or checked_at < min_checked_at):
            min_checked_at = checked_at

    def get_cached(product_id):
        """Returns (raw_bom_or_None, was_already_cached, checked_at)."""
        if force_refresh:
            return None, False, None
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
    level_num = 0
    root_children = []
    frontier = [(root, 1, frozenset({root_id, root["bom_id"]}), root_children, 1.0)]

    while frontier and lookups_done < MAX_LOOKUPS:
        level_num += 1
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
            # Batched (Feb 2026 speedup): one SOAP round-trip per
            # BATCH_CHUNK_SIZE never-before-seen products at this tree
            # level, instead of one call per product - see
            # _fetch_live_batch. A pid absent from the result means its
            # whole chunk failed after retries (real connectivity issue) -
            # left uncached for retry next run, same tolerant behavior the
            # old per-item fetch_and_cache had for a single failed item.
            fetched_batch = _fetch_live_batch(sap_soap_client, uncached_ids, db)
            for pid in uncached_ids:
                if pid in fetched_batch:
                    raw = fetched_batch[pid]
                    _upsert(collection, pid, raw, changed=True)
                    resolved[pid] = raw
                    track(now)
                else:
                    logger.warning(f"BOM fetch failed for '{pid}' (leaving uncached for retry next run)")
                    resolved[pid] = None

        lookups_done += len(to_resolve)

        if progress_cb and uncached_ids:
            # Only report progress for levels that actually hit SAP live -
            # a level served entirely from cache is already fast enough
            # that a UI-facing progress tick would just be visual noise.
            try:
                progress_cb(level_num, lookups_done, MAX_LOOKUPS)
            except Exception:
                pass

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


def refresh_stale_nodes(sap_soap_client, db) -> dict:
    """Background maintenance: for every product_id already in the cache, do
    a lightweight SAP fetch and update only if its BOM revision actually
    changed. Batched (Feb 2026 speedup): BATCH_CHUNK_SIZE products per SOAP
    round-trip instead of one call per product - this job routinely
    re-checks every cached node (potentially thousands), so this is the
    single biggest win of this round's speedup work. A product whose batch
    fails after retries leaves its existing entry untouched (retried next
    cycle) - same "don't erase good data on a transient error" principle as
    everywhere else in this module. Returns {"checked": n, "changed": n,
    "failed": n}."""
    collection = db[COLLECTION_NAME]
    existing_bom_id_by_product = {doc["_id"]: doc.get("bom_id") for doc in collection.find({}, {"_id": 1, "bom_id": 1})}
    product_ids = list(existing_bom_id_by_product.keys())
    stats = {"checked": 0, "changed": 0, "failed": 0}
    if not product_ids:
        return stats

    fetched = _fetch_live_batch(sap_soap_client, product_ids, db)
    for product_id in product_ids:
        if product_id not in fetched:
            logger.warning(f"Background refresh: fetch failed for '{product_id}', keeping existing cache entry")
            stats["failed"] += 1
            continue
        raw = fetched[product_id]
        changed = existing_bom_id_by_product.get(product_id) != (raw["bom_id"] if raw else None)
        _upsert(collection, product_id, raw, changed=changed)
        stats["checked"] += 1
        if changed:
            stats["changed"] += 1
    return stats


def deep_expand_all_known_roots(sap_soap_client, db) -> dict:
    """Nightly (off-peak) full recursive re-walk of every already-known BOM
    root via build_tree_from_cache. An already-fully-explored branch costs
    nothing extra here (pure Mongo cache reads) - this only spends real SAP
    calls discovering genuinely new/deeper connections that a previous,
    SHALLOWER exploration never reached (e.g. a single BOM Explorer search
    only walks what a user expanded on screen, a 2-level report only goes
    2 levels deep, a Purchasing Plan run can be cut short by its own
    per-call MAX_LOOKUPS budget). Confirmed live (Aug 2026): AMF319-B1's
    true structure is 6 levels/95 components deep, but only a shallow
    subset had ever been cached, silently orphaning a genuinely-used
    component (6400-003550) 3 levels down through an intermediate
    sub-assembly (6800-004025) nobody had ever explored on its own - this
    function's whole purpose is to close exactly that class of gap across
    the WHOLE catalog over time, not just page-load-time for one root.
    Returns {"roots_processed": n, "roots_failed": n, "total_roots": n}."""
    collection = db[COLLECTION_NAME]
    root_ids = sorted({d["_id"] for d in collection.find({"found": True}, {"_id": 1})})
    processed, failed = 0, 0
    for root_id in root_ids:
        try:
            build_tree_from_cache(root_id, sap_soap_client, db)
            processed += 1
        except BomFetchError as e:
            logger.warning(f"Nightly deep-expand: root '{root_id}' fetch failed, will retry next run: {e}")
            failed += 1
    return {"roots_processed": processed, "roots_failed": failed, "total_roots": len(root_ids)}
