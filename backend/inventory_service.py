"""Orchestrates the Inventory page: live SAP on-hand stock (with location
breakdown) joined against SAP Standard Costs for valuation, and against
component_master for description/category when SAP's own description isn't
available or a friendlier one already exists locally.

Results are persisted to a small `inventory_cache` Mongo collection (see
get_cached_inventory/refresh_inventory_cache below) so the Inventory page
loads instantly from cache instead of always waiting on a live, multi-minute
SAP pull - a background scheduler (server.py) refreshes this cache every
couple of hours, and a manual "Refresh" button can force it sooner."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from sap_valuation_client import SAPValuationError
from sap_material_client import SAPMaterialError, SAPMaterialAuthError

import bom_cache_service
from bom_cache_service import BomFetchError

logger = logging.getLogger(__name__)

INVENTORY_CACHE_COLLECTION = "inventory_cache"
INVENTORY_CACHE_ID = "latest"
DEEP_BACKFILL_MAX_WORKERS = 6
# Bounds how long a single "Resolve Missing Values" click can run for. On a
# large catalog, checking every never-before-seen item live against SAP (2
# calls x up to 3 retries x up to 30s each - see bom_cache_service._fetch_live)
# can otherwise take hours with no visible progress, which is exactly what
# looked like "stuck" in production. Each item's result is persisted to
# component_master as soon as it resolves (see the executor loop below), so
# stopping early loses nothing - the user just clicks the button again and
# the next run picks up where this one left off (already-resolved ids are
# excluded by _get_deep_backfill_targets).
DEEP_BACKFILL_MAX_RUNTIME_SECONDS = 360


def _resolve_missing_uuids(db, product_ids: list) -> int:
    """Best-effort backfill of product_uuid (needed for Standard Costs
    valuation) for inventory items that have never been captured by a BOM
    Explorer search or Purchasing Plan run - which is most of the catalog,
    since only components discovered as a BOM LEAF get their product_uuid
    saved automatically. Joins purely against whatever's already sitting in
    the local BOM node cache (bom_node_cache) - a product that is itself a
    BOM root now carries its own product_uuid there (see
    sap_soap_client._parse), so this "for free" backfill grows on its own
    as more of the catalog gets explored via BOM Explorer / a Purchasing
    Plan run, or via the periodic BOM cache refresh (bom_cache_service.
    refresh_stale_nodes) re-checking already-cached nodes. Deliberately
    makes NO live SAP calls of its own - this SAP tenant is already prone
    to connection timeouts under concurrent load (see sap_soap_client /
    bom_cache_service docstrings), so a page that's supposed to be reading
    from cache must never itself trigger hundreds of fresh SAP lookups.
    Returns the number of product_ids newly resolved."""
    if not product_ids:
        return 0

    bom_cache = db[bom_cache_service.COLLECTION_NAME]
    comp_cache = db["component_master"]

    resolved = {
        doc["_id"]: doc["product_uuid"]
        for doc in bom_cache.find({"_id": {"$in": product_ids}, "product_uuid": {"$ne": None}}, {"product_uuid": 1})
    }
    for pid, product_uuid in resolved.items():
        comp_cache.update_one({"_id": pid}, {"$set": {"product_uuid": product_uuid}}, upsert=True)

    return len(resolved)


def build_inventory(db, sap_inventory_client, sap_valuation_client) -> list:
    """Returns [{product_id, description, category, total_qty, uom,
    unit_cost, currency, total_value, locations: [{site, logistics_area,
    stock_status, qty}]}] sorted by product_id. If the SAP Standard Costs
    endpoint is temporarily unreachable (it's known to be intermittently
    flaky, see materialvaluationdata connectivity notes elsewhere), this
    degrades gracefully to unit_cost/total_value=None rather than failing
    the whole page - quantities are still useful without a live cost."""
    detail_rows = sap_inventory_client.get_inventory_detail()

    by_product = {}
    for row in detail_rows:
        product_id = row["product_id"]
        entry = by_product.setdefault(product_id, {
            "product_id": product_id,
            "description": row.get("description"),
            "total_qty": 0.0,
            "uom": row.get("uom"),
            "locations": [],
        })
        entry["total_qty"] += row["qty"]
        entry["locations"].append({
            "site": row.get("site"),
            "logistics_area": row.get("logistics_area"),
            "stock_status": row.get("stock_status"),
            "qty": row["qty"],
            "company_code": row.get("company_code"),
            "company_name": row.get("company_name"),
        })

    component_docs = {
        doc["_id"]: doc
        for doc in db["component_master"].find(
            {"_id": {"$in": list(by_product.keys())}}, {"description": 1, "category": 1, "product_uuid": 1}
        )
    }
    for product_id, entry in by_product.items():
        comp = component_docs.get(product_id)
        if comp:
            entry["description"] = comp.get("description") or entry["description"]
            entry["category"] = comp.get("category")
        else:
            entry["category"] = None

    missing_uuid_ids = [pid for pid in by_product if not component_docs.get(pid, {}).get("product_uuid")]
    if missing_uuid_ids:
        newly_resolved = _resolve_missing_uuids(db, missing_uuid_ids)
        if newly_resolved:
            component_docs = {
                doc["_id"]: doc
                for doc in db["component_master"].find(
                    {"_id": {"$in": list(by_product.keys())}}, {"description": 1, "category": 1, "product_uuid": 1}
                )
            }

    product_uuids = [
        component_docs[pid]["product_uuid"]
        for pid in by_product
        if component_docs.get(pid, {}).get("product_uuid")
    ]

    costs = {}
    if product_uuids:
        try:
            costs = sap_valuation_client.get_standard_costs(product_uuids)
        except (SAPValuationError, requests.exceptions.RequestException) as e:
            logger.warning(f"Standard Costs unavailable for Inventory valuation, showing quantities only: {e}")

    # A single flaky batch in get_standard_costs makes the WHOLE call raise
    # (no partial results) - on this fragile tenant that happens often
    # enough that falling back to unit_cost=None across the board would
    # regularly wipe out valuation data that was already resolved in a
    # previous, successful refresh cycle. So valuation is "sticky": if this
    # round's live cost lookup didn't return a price for an item, keep
    # whatever was cached last time instead of blanking it - a genuinely
    # new price from SAP still overwrites it normally.
    previous_by_id = {it["product_id"]: it for it in get_cached_inventory(db)["items"]}

    for product_id, entry in by_product.items():
        product_uuid = component_docs.get(product_id, {}).get("product_uuid")
        cost = costs.get(product_uuid.upper()) if product_uuid else None
        if cost:
            entry["unit_cost"] = cost["amount"]
            entry["currency"] = cost["currency"]
            entry["total_value"] = round(cost["amount"] * entry["total_qty"], 2)
        else:
            previous = previous_by_id.get(product_id)
            if previous and previous.get("unit_cost") is not None:
                entry["unit_cost"] = previous["unit_cost"]
                entry["currency"] = previous["currency"]
                entry["total_value"] = round(previous["unit_cost"] * entry["total_qty"], 2)
            else:
                entry["unit_cost"] = None
                entry["currency"] = None
                entry["total_value"] = None
        entry["locations"].sort(key=lambda loc: -loc["qty"])

    return sorted(by_product.values(), key=lambda e: e["product_id"])


def _compute_no_bom_flags(db) -> dict:
    """Scans the whole bom_node_cache collection (fast, ~20ms at current
    3800-doc scale) to flag product_ids that are structurally orphaned in
    the BOM graph: not a BOM root themselves (SAP confirms no production
    BOM), AND never seen as a component/leaf inside anyone else's CURRENT
    ACTIVE BOM either. A genuine raw material actively used in production
    will show up as a leaf somewhere and won't be flagged; a Finished
    Good/Sub-Assembly missing its BOM (a real data gap) will be. This is
    deliberately "active BOM only" per user decision (Aug 2026) - a
    component that only ever appeared in a genuinely SUPERSEDED (non-
    Consistent) BOM revision still gets flagged here (see
    historical_leaf_ids below for the separate transparency note instead
    of silently clearing the flag). A component that's only used in an
    ALTERNATE Consistent BOM (not the single canonical `groups` tree, see
    sap_soap_client._build_bom_from_hit_blocks's active_alternate_ids) DOES
    count as active per user confirmation (Aug 2026: "any item can have
    multiple consistent BOMs, they are all active") - folded into
    `leaf_ids` below, not `historical_leaf_ids`.
    Returns {product_id: bool}."""
    bom_cache = db[bom_cache_service.COLLECTION_NAME]
    roots_with_bom = {d["_id"] for d in bom_cache.find({"found": True}, {"_id": 1})}
    leaf_ids = set()
    historical_leaf_ids = set()
    for doc in bom_cache.find({}, {"groups": 1, "historical_input_ids": 1, "active_alternate_ids": 1}):
        for group in doc.get("groups", []):
            for item in group.get("items", []):
                pid = item.get("product_id")
                if pid:
                    leaf_ids.add(pid)
        leaf_ids.update(doc.get("active_alternate_ids") or [])
        historical_leaf_ids.update(doc.get("historical_input_ids") or [])
    return {"roots_with_bom": roots_with_bom, "leaf_ids": leaf_ids, "historical_leaf_ids": historical_leaf_ids}


def get_cached_inventory(db):
    """Instant read of the last-refreshed inventory snapshot - what the
    Inventory page loads on every visit. Returns
    {"items": [...], "categories": [...], "updated_at": datetime | None}
    with empty items/None updated_at if no refresh has ever completed yet
    (first-ever startup, before the background scheduler's first cycle).

    Category is re-joined from component_master on every read (not just
    trusted from whatever was baked into the snapshot at the last live SAP
    refresh) - a manual edit, AI re-categorize, or bulk Categorize All can
    happen at any time independently of the next SAP refresh, and must show
    up immediately here without needing one. The "no BOM" flag is likewise
    computed live from the current bom_node_cache state on every read (see
    _compute_no_bom_flags) rather than baked into the snapshot, so it
    reflects the latest BOM Explorer/Purchasing Plan exploration without
    needing a full inventory refresh."""
    doc = db[INVENTORY_CACHE_COLLECTION].find_one({"_id": INVENTORY_CACHE_ID})
    if not doc:
        return {"items": [], "categories": [], "updated_at": None}
    items = doc.get("items", [])
    if items:
        current_categories = {
            c["_id"]: c.get("category")
            for c in db["component_master"].find(
                {"_id": {"$in": [it["product_id"] for it in items]}}, {"category": 1}
            )
        }
        bom_membership = _compute_no_bom_flags(db)
        for it in items:
            it["category"] = current_categories.get(it["product_id"])
            it["no_bom"] = (
                it["product_id"] not in bom_membership["roots_with_bom"]
                and it["product_id"] not in bom_membership["leaf_ids"]
            )
            # Informational only (see _compute_no_bom_flags docstring) -
            # doesn't affect no_bom or the filter checkbox, just lets the
            # UI show "found in a historical/superseded BOM" for
            # transparency on an otherwise-flagged item.
            it["historical_bom"] = it["no_bom"] and it["product_id"] in bom_membership["historical_leaf_ids"]
    categories = sorted({it["category"] for it in items if it.get("category")})
    return {"items": items, "categories": categories, "updated_at": doc.get("updated_at")}


def refresh_inventory_cache(db, sap_inventory_client, sap_valuation_client) -> dict:
    """Pulls a fresh live snapshot (build_inventory) and overwrites the
    single cached document - called both by the manual "Refresh" button
    and the 2-hourly background scheduler (see server.py)."""
    items = build_inventory(db, sap_inventory_client, sap_valuation_client)
    categories = sorted({it["category"] for it in items if it.get("category")})
    updated_at = datetime.now(timezone.utc)
    db[INVENTORY_CACHE_COLLECTION].update_one(
        {"_id": INVENTORY_CACHE_ID},
        {"$set": {"items": items, "categories": categories, "updated_at": updated_at}},
        upsert=True,
    )
    return {"items": items, "categories": categories, "updated_at": updated_at}


def deep_backfill_uuids(db, sap_soap_client, sap_material_client=None, progress_callback=None) -> dict:
    """User-requested, one-time CONTROLLED live SAP lookup for every
    inventory item that still has no product_uuid after the free,
    zero-API-call join in _resolve_missing_uuids. Two-step resolution per
    item, throttled (DEEP_BACKFILL_MAX_WORKERS) since this SAP tenant is
    known to hit connection timeouts under load - the user explicitly chose
    "controlled backfill, accept it'll add some load" over waiting
    indefinitely for organic coverage growth:
      1. BOM-based (same as before): is this product a BOM root, or has it
         already been checked before? Free/cheap, reuses bom_node_cache.
      2. NEW - direct Material Master lookup (sap_material_client,
         QueryMaterialIn): for anything step 1 couldn't resolve (no BOM
         relationship anywhere) - this is the ONLY way to get a UUID for a
         pure raw material that's never appeared as anyone's ingredient.
         As of Feb 2026 this requires a SAP authorization grant that may
         not be in place yet (see sap_material_client module docstring) -
         if so, the first call raises SAPMaterialAuthError and this
         function stops attempting step 2 for the rest of the run (no
         point retrying a guaranteed failure on every item), surfaced via
         result["material_lookup_unauthorized"].
    Bounded to DEEP_BACKFILL_MAX_RUNTIME_SECONDS of wall-clock time - on a
    large backlog this stops early rather than running for hours, reporting
    "stopped_early" so the caller can tell the user to just run it again to
    pick up the rest (nothing already resolved is lost or re-checked).
    Returns {"total", "resolved", "still_missing", "material_lookup_unauthorized", "stopped_early"}."""
    target_ids = _get_deep_backfill_targets(db)
    total = len(target_ids)
    if progress_callback:
        progress_callback(0, total)
    if total == 0:
        return {"total": 0, "resolved": 0, "still_missing": 0, "material_lookup_unauthorized": False, "stopped_early": False}

    bom_cache = db[bom_cache_service.COLLECTION_NAME]
    comp_cache = db["component_master"]
    already_checked_boms = {
        doc["_id"]: doc.get("product_uuid")
        for doc in bom_cache.find({"_id": {"$in": target_ids}}, {"product_uuid": 1})
    }
    resolved = 0
    processed = 0
    auth_error_seen = [False]  # list so the closure below can mutate it

    def fetch_one(pid):
        if pid in already_checked_boms:
            # Already resolved for free by a previous BOM Explorer/Purchasing
            # Plan run or backfill cycle - just reuse it, no lookup needed.
            if already_checked_boms[pid]:
                return pid, already_checked_boms[pid]
        else:
            try:
                raw = bom_cache_service._fetch_live(sap_soap_client, pid, db)
            except BomFetchError as e:
                logger.warning(f"Deep UUID backfill: BOM lookup failed for '{pid}' after retries: {e}")
                raw = None
            else:
                bom_cache_service._upsert(bom_cache, pid, raw, changed=True)
            if raw and raw.get("product_uuid"):
                return pid, raw["product_uuid"]

        if auth_error_seen[0] or sap_material_client is None:
            return pid, None
        try:
            return pid, sap_material_client.resolve_uuid(pid)
        except SAPMaterialAuthError as e:
            auth_error_seen[0] = True
            logger.error(f"Deep UUID backfill: SAP rejected the direct Material lookup (missing authorization) - skipping it for the rest of this run: {e}")
            return pid, None
        except SAPMaterialError as e:
            logger.warning(f"Deep UUID backfill: direct Material lookup failed for '{pid}': {e}")
            return pid, None
        except requests.exceptions.RequestException as e:
            # A transient connection/timeout issue (this tenant is prone to
            # them under load) - just leave this one item unresolved for
            # this run rather than letting it crash the entire batch and
            # lose every already-processed result (that's what happened
            # before this fix: one mid-batch timeout took down a job that
            # had already correctly resolved 224 other items).
            logger.warning(f"Deep UUID backfill: direct Material lookup network error for '{pid}', leaving unresolved this round: {e}")
            return pid, None

    deadline = time.monotonic() + DEEP_BACKFILL_MAX_RUNTIME_SECONDS
    stopped_early = False
    executor = ThreadPoolExecutor(max_workers=DEEP_BACKFILL_MAX_WORKERS)
    try:
        futures = {executor.submit(fetch_one, pid): pid for pid in target_ids}
        for future in as_completed(futures):
            if future.cancelled():
                # Never started (cancelled after the time budget ran out
                # below) - don't count it, it did no work.
                continue
            _, product_uuid = future.result()
            processed += 1
            if product_uuid:
                comp_cache.update_one({"_id": futures[future]}, {"$set": {"product_uuid": product_uuid}}, upsert=True)
                resolved += 1
            if progress_callback:
                progress_callback(processed, total)
            if not stopped_early and time.monotonic() > deadline:
                # Time budget used up - stop handing out new work. Futures
                # already submitted but not yet started get cancelled;
                # ones already in-flight are left to finish in the
                # background (their bom_node_cache/component_master writes
                # still land, they're just no longer counted here) rather
                # than blocking this call waiting on a possibly-hung SAP
                # request.
                stopped_early = True
                for f in futures:
                    f.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return {
        "total": total, "resolved": resolved, "still_missing": total - resolved,
        "material_lookup_unauthorized": auth_error_seen[0], "stopped_early": stopped_early,
    }


def _get_deep_backfill_targets(db) -> list:
    """Every product_id sitting in the current inventory snapshot that has
    no product_uuid yet in component_master - regardless of whether it was
    already checked in bom_node_cache before, since a previous "no BOM
    found" result no longer means "unresolvable" now that the direct
    Material lookup (step 2 in deep_backfill_uuids) doesn't depend on a BOM
    relationship at all."""
    cached = get_cached_inventory(db)
    product_ids = [it["product_id"] for it in cached["items"]]
    if not product_ids:
        return []

    has_uuid = {
        doc["_id"]
        for doc in db["component_master"].find({"_id": {"$in": product_ids}, "product_uuid": {"$ne": None}}, {"_id": 1})
    }
    return [pid for pid in product_ids if pid not in has_uuid]
