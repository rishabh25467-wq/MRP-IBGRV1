"""Supplier Portal Phase 3 (shipment 2-way match) + Phase 4 (internal
GRN approval -> automated SAP Goods Receipt) - Aug 2026.

Phase 3: an approved vendor builds a "cart" of line items (across one or
MORE Purchase Orders - user's explicit ask, Aug 28 2026) from their
cached open-PO list, confirms it, and gets ONE exclusive 6-char
alphanumeric doc code (ambiguous 0/O/1/I excluded) covering the whole
cart. Validated so the shipped quantity never exceeds what's still open
on each PO item across ALL of that item's non-rejected shipments (the
"2-way match": PO qty vs shipment qty). A shipment's contents can be
freely edited (add/remove items, change qty) via `update_shipment_items`
for as long as it stays `status="in_transit"` - the moment internal
staff Approve or Reject it, it's locked (user's explicit ask).

Phase 4: internal staff enter that code, physically match goods +
supplier invoice, and Approve - which immediately records the approval
(bill date + staff-CONFIRMED actual received qty per line, which the
GRN screen collects separately from the vendor's own claimed ship_qty -
user's explicit ask, Aug 29 2026 - since what's on the truck vs what
was physically counted in can differ) and kicks off a 2-STEP live SAP
write as a background job (Playwright is too slow to block the request,
same pattern as every other UI-automation write in this app): (1) posts
the real Goods Receipt via sap_playwright_supplier_pgr_service (SAP UI
automation - see that module's docstring for why: the GSA API write
this used before, sap_gsa_write_client, is CONFIRMED to only work for
non-stock/service PO lines, never real stock materials), ONE call PER
distinct PO number in the shipment (grouping every line item of that PO
into a single submission, matching the user's own manual flow exactly -
a shipment can span multiple POs), then (2) - since the GSA-era Goods
Movement approach carries over unchanged here - a follow-up Goods
Movement (sap_goods_movement_client, reused from Store Approval) moves
each line's STAFF-CONFIRMED actual qty (not the vendor's ship_qty) into
the receiver's chosen warehouse. NOTE (confirmed live, Aug 28 2026):
forcing an explicit RESTRICTED/Quality-Inspection stock status on that
movement is NOT achievable on this tenant as currently configured - see
_post_goods_movement_for_items's docstring below for the live test that
proved this; step 2 today only does a PLAIN move into the chosen
warehouse. The internal approval itself is NEVER blocked by SAP being
unreachable - `sap_sync_status`/`sap_movement_status` track each step
separately so staff always know exactly how far a shipment got.

Invoice creation (a separate SAP screen, Supplier Invoicing work center)
is explicitly OUT OF SCOPE here (user's explicit ask, Aug 29 2026) - a
later phase, done by a different user.

Discrepancy handling (Aug 28 2026, user's explicit ask): the RECEIVING
STORE staff (not the vendor) mark a mismatch with a free-text reason +
the specific line item(s) that don't match - status becomes
"discrepancy", no SAP write happens. The intended resolution path is the
store calling the vendor, who edits their own shipment (still allowed
while status="discrepancy") - any such edit automatically clears the
discrepancy and puts the shipment back to "in_transit" for re-review.
Staff can also still directly Approve/Reject straight from "discrepancy"
if they decide to override rather than wait for an edit.
"""
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

import inventory_service
import sap_po_client
from sap_playwright_supplier_pgr_service import _build_notification_id
from sap_wip_clearing_client import company_and_set_of_books_for_site

# Sep 17 2026, Manual GRN feature - the auth_users collection name is
# duplicated here as a plain string (NOT `import auth_service`) because
# server.py imports this module BEFORE load_dotenv() runs, and
# auth_service.py reads AZURE_AD_* env vars at module level - importing
# it this early would crash with a KeyError. Must stay in sync with
# auth_service.USERS_COLLECTION.
AUTH_USERS_COLLECTION = "auth_users"

PO_CACHE_COLLECTION = "supplier_portal_po_cache"
SAP_OPEN_QTY_COLLECTION = "sap_po_open_qty_cache"
SAP_PO_NUMBER_COLLECTION = "sap_po_custom_number_cache"
SHIPMENTS_COLLECTION = "supplier_portal_shipments"

# Background refresh runs every 10 min (server.py's
# start_supplier_po_cache_refresh_loop) - a row must survive 3
# consecutive misses (~30 min) before it's actually deleted, so a
# single fetch cycle's own window/timing gap can never be mistaken for
# "PO closed" (deployment-scan-flagged data-loss risk, fixed 2026-08-28).
STALE_CYCLES_BEFORE_DELETE = 3
REFRESH_INTERVAL_MINUTES = 10


class ShipmentError(Exception):
    pass


class ShipmentValidationError(ShipmentError):
    pass


class ShipmentNotFoundError(ShipmentError):
    pass


def ensure_indexes(db) -> None:
    db[SHIPMENTS_COLLECTION].create_index("vendor_code")
    db[SHIPMENTS_COLLECTION].create_index("items.po_number")
    db[PO_CACHE_COLLECTION].create_index("vendor_code")


def expire_po_cache(db, po_number: str, item_number: str = None) -> int:
    """Sep 14 2026 - after a real SAP cancellation succeeds, immediately
    drop the cancelled PO (or just the one item) from the local cache
    instead of waiting on the next background refresh cycle - same
    immediacy concern as the Open PO Qty targeted-refresh fix above."""
    query = {"po_number": po_number, "expired": {"$ne": True}}
    if item_number:
        query["item_number"] = item_number
    return db[PO_CACHE_COLLECTION].update_many(query, {"$set": {"expired": True}}).modified_count


def refresh_po_cache(db, vendor_code: str, items: list, lower_bound: int = 0) -> None:
    """Called every time a LIVE SAP PO fetch succeeds - keeps a durable
    local copy so the vendor's open-PO list (and shipment creation) still
    works between SAP calls, and so a vendor always sees "last known"
    data instead of a hard error the moment SAP itself is briefly down.

    Also EXPIRES any of this vendor's previously-cached rows that are no
    longer in the fresh live fetch (PO now Finished, or genuinely gone) -
    a live fetch is the source of truth, so a stale/no-longer-open row
    must not linger forever (found by user report Aug 28 2026).

    Sep 14 2026 CRITICAL FIX (real incident - vendor P3267's genuinely
    "In Process" POs 27601/28255/28467/29027/29073 vanished from their
    dashboard): a cached row whose `po_number` is <= `lower_bound` (i.e.
    OLDER than what this fetch's recency window even looked at - see
    sap_po_client.py's fetch_recent_window docstring) must NEVER be
    treated as "missing" - we simply have no fresh information about it
    this cycle, which is not the same as SAP saying it's gone. Only rows
    that WERE inside the window this fetch actually covered and still
    didn't show up are genuinely "missing" candidates. The independent
    backfill pointer (sap_po_client.fetch_backfill_chunk +
    merge_backfill_rows below) is what eventually re-confirms these
    older rows one way or the other.

    A row that IS inside the window is only MARKED (`missing_since`) on
    its first miss, and only actually deleted once it's been missing for
    STALE_CYCLES_BEFORE_DELETE consecutive refreshes - protects against
    a single fetch cycle's own window/timing hiccup being mistaken for
    "PO closed" (fixed 2026-08-28). A row that reappears in a later
    fetch has `missing_since` cleared automatically via the `$set` below.

    Only rows tagged `source="sap_live"` are eligible for this at all -
    a manually-seeded demo/test fixture row (e.g. dummy vendor_code
    S9999's fixture, which never comes from a real SAP fetch) has no
    `source` field and is left alone, otherwise the global background
    refresh would wipe it out every cycle (found by testing_agent,
    iteration 122)."""
    now = datetime.now(timezone.utc)
    fresh_keys = []
    for it in items:
        key = f"{vendor_code}::{it['po_number']}::{it['item_number']}"
        fresh_keys.append(key)
        db[PO_CACHE_COLLECTION].update_one(
            {"_id": key},
            {"$set": {**it, "vendor_code": vendor_code, "source": "sap_live", "updated_at": now, "missing_since": None, "expired": False}},
            upsert=True,
        )
    missing_query = {"vendor_code": vendor_code, "source": "sap_live", "_id": {"$nin": fresh_keys}}
    candidates = list(db[PO_CACHE_COLLECTION].find(missing_query, {"_id": 1, "po_number": 1}))
    in_window_missing_ids = []
    for c in candidates:
        try:
            still_in_window = int(c.get("po_number")) > lower_bound
        except (TypeError, ValueError):
            still_in_window = True  # defensive - unexpected non-numeric ID, don't silently skip it
        if still_in_window:
            in_window_missing_ids.append(c["_id"])
    if not in_window_missing_ids:
        return
    db[PO_CACHE_COLLECTION].update_many(
        {"_id": {"$in": in_window_missing_ids}, "missing_since": None}, {"$set": {"missing_since": now}},
    )
    stale_cutoff = now - timedelta(minutes=STALE_CYCLES_BEFORE_DELETE * REFRESH_INTERVAL_MINUTES)
    # Soft-delete only (deployment-scan-flagged, fixed 2026-08-29): an unattended background
    # loop must never hard-delete rows outright, even ones scoped this narrowly - mark them
    # `expired` instead so a vendor's PO disappears from their open-PO list (see the
    # `expired` filters in get_cached_pos_with_remaining/_resolve_items below) without losing
    # the underlying record for audit/debugging.
    db[PO_CACHE_COLLECTION].update_many(
        {"_id": {"$in": in_window_missing_ids}, "missing_since": {"$lte": stale_cutoff}},
        {"$set": {"expired": True, "expired_at": now}},
    )


def seed_po_cache_items(db, vendor_code: str, items: list) -> None:
    """Sep 10 2026, user's explicit ask: a PO created via THIS app's own
    PO Creation flow used to take up to REFRESH_INTERVAL_MINUTES (10 min)
    to appear on the Supplier Dashboard / Open Purchase Orders page,
    because those only ever read PO_CACHE_COLLECTION, which itself only
    updates via the shared background SAP poll (refresh_all_vendor_caches,
    server.py) - deliberately NOT done live on every page load (too
    slow/unstable per that loop's own docstring). We already have this
    PO's full item data right here at creation time - upsert it
    immediately so it's visible right away. Unlike refresh_po_cache,
    this NEVER touches/expires any of the vendor's OTHER cached rows
    (this is a single-PO seed, not a full vendor resync) - the next
    background cycle's real SAP read is still the source of truth and
    will just overwrite this with itself."""
    now = datetime.now(timezone.utc)
    for it in items:
        key = f"{vendor_code}::{it['po_number']}::{it['item_number']}"
        db[PO_CACHE_COLLECTION].update_one(
            {"_id": key},
            {"$set": {**it, "vendor_code": vendor_code, "source": "sap_live", "updated_at": now, "missing_since": None, "expired": False}},
            upsert=True,
        )


def refresh_all_vendor_caches(db, rows: list, lower_bound: int = 0) -> dict:
    """Fans sap_po_client.fetch_recent_window's single global batch out
    per vendor (called from server.py's background refresh loop) - every
    vendor_code seen in this batch gets its cache updated, AND every
    vendor_code already in the cache gets re-checked even with an empty
    list, so a vendor whose open POs all disappeared this cycle (now
    genuinely Finished/Cancelled) ends up with an empty cache instead of
    a stale one. `lower_bound` (Sep 14 2026 fix) is passed straight
    through to refresh_po_cache so a cached row OLDER than this fetch's
    own window is never wrongly treated as "missing" - see that
    function's docstring."""
    grouped = {}
    for r in rows:
        grouped.setdefault(r["vendor_code"], []).append(r)
    already_cached_vendors = db[PO_CACHE_COLLECTION].distinct("vendor_code")
    for vendor_code in set(grouped) | set(already_cached_vendors):
        refresh_po_cache(db, vendor_code, grouped.get(vendor_code, []), lower_bound)
    return {"vendors_updated": len(grouped), "total_line_items": len(rows)}


def merge_backfill_rows(db, rows: list) -> dict:
    """Sep 14 2026 fix - merges sap_po_client.fetch_backfill_chunk's
    older-PO results into the cache. Deliberately reuses seed_po_cache_items
    (upsert-only, never expires anything) since a backfill chunk only
    ever covers a narrow ID slice, never a vendor's full PO list - unlike
    refresh_all_vendor_caches above, absence here means nothing at all."""
    grouped = {}
    for r in rows:
        grouped.setdefault(r["vendor_code"], []).append(r)
    for vendor_code, items in grouped.items():
        seed_po_cache_items(db, vendor_code, items)
    return {"vendors_touched": len(grouped), "total_line_items": len(rows)}


def vendor_directory(db, search: str = "", limit: int = 20) -> list:
    """TESTING-ONLY helper (see server.py's SUPPLIER_PORTAL_TESTING_MODE
    gate) - lets a tester search cached vendor_code/vendor_name pairs to
    impersonate on the dashboard. Remove this + its route before launch."""
    query = {}
    search = (search or "").strip()
    if search:
        query["$or"] = [
            {"vendor_code": {"$regex": search, "$options": "i"}},
            {"vendor_name": {"$regex": search, "$options": "i"}},
        ]
    pipeline = [
        {"$match": query},
        {"$group": {"_id": "$vendor_code", "vendor_name": {"$first": "$vendor_name"}}},
        {"$sort": {"_id": 1}},
        {"$limit": limit},
    ]
    return [{"vendor_code": d["_id"], "vendor_name": d.get("vendor_name")} for d in db[PO_CACHE_COLLECTION].aggregate(pipeline)]


def _shipped_qty_so_far(db, vendor_code: str, po_number: str, item_number: str, exclude_doc_code: str = None) -> float:
    match = {"vendor_code": vendor_code, "status": {"$in": ["in_transit", "approved"]}}
    if exclude_doc_code:
        match["_id"] = {"$ne": exclude_doc_code}
    pipeline = [
        {"$match": match},
        {"$unwind": "$items"},
        {"$match": {"items.po_number": po_number, "items.item_number": item_number}},
        {"$group": {"_id": None, "total": {"$sum": "$items.ship_qty"}}},
    ]
    result = list(db[SHIPMENTS_COLLECTION].aggregate(pipeline))
    return result[0]["total"] if result else 0.0


def _qty_breakdown_by_status(db, vendor_code: str, po_number: str, item_number: str, exclude_doc_code: str = None) -> dict:
    """Sep 9 2026, user's explicit ask: split what `_shipped_qty_so_far`
    used to lump together into "in_transit_qty" (this vendor's OWN
    shipments still awaiting GRN approval - SAP doesn't know about these
    yet) vs. "approved_qty" (received via THIS app, used only as a
    fallback for received_qty below before SAP's own report has ever
    been read for this item - see _compute_qty_state)."""
    match = {"vendor_code": vendor_code, "status": {"$in": ["in_transit", "approved"]}}
    if exclude_doc_code:
        match["_id"] = {"$ne": exclude_doc_code}
    pipeline = [
        {"$match": match},
        {"$unwind": "$items"},
        {"$match": {"items.po_number": po_number, "items.item_number": item_number}},
        {"$group": {"_id": "$status", "total": {"$sum": "$items.ship_qty"}}},
    ]
    totals = {"in_transit": 0.0, "approved": 0.0}
    for row in db[SHIPMENTS_COLLECTION].aggregate(pipeline):
        totals[row["_id"]] = row["total"]
    return {"in_transit_qty": totals["in_transit"], "approved_qty": totals["approved"]}


def _compute_qty_state(db, vendor_code: str, po_number: str, item_number: str, po_qty: float, exclude_doc_code: str = None) -> dict:
    """Sep 9 2026, user's explicit ask: "In Transit Qty"/"Received
    Qty"/"Open Qty" columns on the Supplier Dashboard, PLUS the bug fix
    this surfaced - shipment-creation validation used to check the new
    ship_qty against SAP's own Open Qty report ALONE whenever it was
    cached, silently ignoring the vendor's own already-in-transit (not
    yet SAP-received) shipments for that same item. That let a vendor
    create two overlapping in-transit shipments that together exceeded
    the PO's real open quantity, because neither one had been received
    by SAP yet to bring its own Open Qty figure down. Both
    get_cached_pos_with_remaining() (display) and _resolve_items()
    (validation) now share this one calculation so they can never
    disagree with each other again.

    `received_qty` prefers SAP's own report (po_qty - sap_open_qty,
    firm/closed, includes receipts posted outside this app) once it's
    been read at least once for this item; falls back to this app's own
    locally-recorded "approved" shipments only until then.
    `in_transit_qty` is always computed locally - SAP has no visibility
    into a shipment the vendor hasn't had received yet."""
    breakdown = _qty_breakdown_by_status(db, vendor_code, po_number, item_number, exclude_doc_code=exclude_doc_code)
    in_transit_qty = breakdown["in_transit_qty"]
    sap_cached = get_sap_open_qty(db, po_number, item_number)
    if sap_cached:
        received_qty = round((po_qty or 0) - sap_cached["open_qty"], 4)
        sap_verified_at = sap_cached["fetched_at"]
    else:
        received_qty = breakdown["approved_qty"]
        sap_verified_at = None
    remaining_qty = round((po_qty or 0) - received_qty - in_transit_qty, 4)
    return {
        "in_transit_qty": in_transit_qty,
        "received_qty": received_qty,
        "remaining_qty": max(remaining_qty, 0.0),
        "sap_verified_at": sap_verified_at,
    }


def get_sap_open_qty(db, po_number: str, item_number: str) -> dict:
    """Sep 2 2026, user's explicit ask: "Open PO qty should be fetched
    from SAP, not just maintained locally... to ensure if shipments
    have been received outside this app, we keep count." Backed by
    `sap_playwright_supplier_pgr_service.fetch_open_po_quantities()`
    (read-only SAP UI read, refreshed periodically in the background -
    see server.py's start_sap_open_qty_refresh_loop) into
    SAP_OPEN_QTY_COLLECTION. Returns None if this PO/item hasn't been
    read yet (never seen it, or read failed every cycle so far) - the
    caller must fall back to the old locally-computed value, NEVER
    treat "not cached yet" as zero-open."""
    return db[SAP_OPEN_QTY_COLLECTION].find_one({"_id": f"{po_number}:{item_number}"})


def attach_po_pricing(db, vendor_code: str, items: list) -> None:
    """Sep 9 2026, user's explicit ask: "show PO price also to the GRN
    person" - mutates each item in place with `unit_price`/`currency`
    from the PO cache (already captured from SAP's NetUnitPrice on every
    PO pull, just never surfaced on the GRN screen before) - a PO/item
    combo not found in the cache (very old shipment, cache entry purged)
    simply leaves unit_price as None, the frontend then just omits that
    column for that row rather than showing a wrong number."""
    for it in items:
        cached = db[PO_CACHE_COLLECTION].find_one({"_id": f"{vendor_code}::{it.get('po_number')}::{it.get('item_number')}"})
        it["unit_price"] = cached.get("unit_price") if cached else None
        it["currency"] = cached.get("currency") if cached else None


def list_active_po_numbers(db) -> list:
    """Distinct PO numbers currently in the vendor PO cache (any vendor,
    not expired) - the background refresh loop's own worklist."""
    return [r["_id"] for r in db[PO_CACHE_COLLECTION].aggregate([
        {"$match": {"expired": {"$ne": True}}},
        {"$group": {"_id": "$po_number"}},
    ])]


def list_active_po_numbers_for_vendor(db, vendor_code: str) -> list:
    """Sep 9 2026, user's explicit ask: "can it just [refresh] when
    supplier refreshes their page" - same worklist as
    list_active_po_numbers() above, scoped to just ONE vendor so the
    Supplier Dashboard can trigger its own fast, targeted live SAP pull
    on every page load instead of waiting for the shared background
    loop's ~5-6 min cycle to get around to it."""
    return [r["_id"] for r in db[PO_CACHE_COLLECTION].aggregate([
        {"$match": {"vendor_code": vendor_code, "expired": {"$ne": True}}},
        {"$group": {"_id": "$po_number"}},
    ])]


def store_sap_open_qty_cache(db, results: dict) -> dict:
    """results: {po_number: {item_number: {po_qty, delivered_qty,
    open_qty, delivery_completed}}} - from fetch_open_po_quantities().
    Upserts each PO/item pair with a fresh `fetched_at` - a PO that
    couldn't be read this cycle simply keeps its last-known cached
    value (see get_sap_open_qty's docstring)."""
    now = datetime.now(timezone.utc)
    updated = 0
    for po_number, items in results.items():
        for item_number, vals in items.items():
            db[SAP_OPEN_QTY_COLLECTION].update_one(
                {"_id": f"{po_number}:{item_number}"},
                {"$set": {"po_number": po_number, "item_number": item_number, "fetched_at": now, **vals}},
                upsert=True,
            )
            updated += 1
    return {"pos_updated": len(results), "items_updated": updated}


def get_cached_pos_with_remaining(db, vendor_code: str) -> list:
    items = list(db[PO_CACHE_COLLECTION].find({"vendor_code": vendor_code, "expired": {"$ne": True}}, {"_id": 0}).sort("po_number", 1))
    for it in items:
        state = _compute_qty_state(db, vendor_code, it["po_number"], it["item_number"], it.get("po_qty") or 0)
        it["already_shipped_qty"] = _shipped_qty_so_far(db, vendor_code, it["po_number"], it["item_number"])
        it["in_transit_qty"] = state["in_transit_qty"]
        it["received_qty"] = state["received_qty"]
        it["remaining_qty"] = state["remaining_qty"]
        it["sap_verified_at"] = state["sap_verified_at"]
        it["buyer_entity_name"] = sap_po_client.buyer_entity_name(it.get("buyer_code"))
    attach_sap_po_numbers(db, items)
    return items


def get_cached_po_by_number(db, po_number: str) -> list:
    """Sep 14 2026, user's explicit ask: let internal staff search the
    Open Purchase Orders page by PO number directly, not only by
    picking a vendor first. Same enrichment as
    `get_cached_pos_with_remaining`, just filtered by `po_number`
    (across any vendor, cache `_id` isn't scoped to one) instead of
    `vendor_code` - each cached row already carries its own
    `vendor_code`, used per-item here since a lookup by number spans
    whichever vendor that PO actually belongs to."""
    items = list(db[PO_CACHE_COLLECTION].find({"po_number": po_number, "expired": {"$ne": True}}, {"_id": 0}).sort("item_number", 1))
    for it in items:
        state = _compute_qty_state(db, it["vendor_code"], it["po_number"], it["item_number"], it.get("po_qty") or 0)
        it["already_shipped_qty"] = _shipped_qty_so_far(db, it["vendor_code"], it["po_number"], it["item_number"])
        it["in_transit_qty"] = state["in_transit_qty"]
        it["received_qty"] = state["received_qty"]
        it["remaining_qty"] = state["remaining_qty"]
        it["sap_verified_at"] = state["sap_verified_at"]
        it["buyer_entity_name"] = sap_po_client.buyer_entity_name(it.get("buyer_code"))
    attach_sap_po_numbers(db, items)
    return items


def get_sap_po_number(db, po_number: str) -> str:
    """Sep 10 2026, user's explicit ask: the tenant's own custom
    "Purchase Order Number" field (e.g. 'P1PO-00641/26-27', the number
    printed on the physical PO document) - see
    sap_po_write_client.SAPPurchaseOrderWriteClient.get_purchase_order_number
    for how this is actually fetched from SAP (a different field from
    the plain sequential PurchaseOrderID this app tracks everywhere
    else). Returns None if never successfully fetched yet (background
    loop catches up, see server.py) - callers must treat None as "not
    known yet", never as "genuinely blank"."""
    doc = db[SAP_PO_NUMBER_COLLECTION].find_one({"_id": po_number})
    return doc.get("sap_po_number") if doc else None


def store_sap_po_number(db, po_number: str, sap_po_number: str) -> None:
    db[SAP_PO_NUMBER_COLLECTION].update_one(
        {"_id": po_number},
        {"$set": {"sap_po_number": sap_po_number, "fetched_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def attach_sap_po_numbers(db, items: list) -> None:
    """Mutates each item in place with `sap_po_number` (joined from
    SAP_PO_NUMBER_COLLECTION by po_number) - a PO not yet fetched simply
    leaves it None, the frontend then just omits it for that row rather
    than showing a wrong/blank value."""
    po_numbers = {it.get("po_number") for it in items if it.get("po_number")}
    cache = {d["_id"]: d.get("sap_po_number") for d in db[SAP_PO_NUMBER_COLLECTION].find({"_id": {"$in": list(po_numbers)}})}
    for it in items:
        it["sap_po_number"] = cache.get(it.get("po_number"))


def ensure_sap_po_numbers_live(db, items: list, sap_po_write_client) -> None:
    """Sep 10 2026, user's explicit ask: "need this in the GRN window
    also while lookup process" - the background catch-up loop
    (server.py) only crawls ~15 POs/5min, so a shipment on a PO that
    hasn't been reached yet showed nothing at lookup time. A single GRN
    lookup only ever touches a HANDFUL of distinct POs (unlike the list
    endpoints, which stay cache-only on purpose to stay fast), so it's
    safe to do a live SAP fetch here for just the ones still missing -
    attaches the result immediately AND caches it for every other
    screen. Best-effort per PO (get_purchase_order_number never raises),
    one PO's SAP hiccup can't block the others."""
    attach_sap_po_numbers(db, items)
    missing = {it["po_number"] for it in items if it.get("po_number") and it.get("sap_po_number") is None}
    for po_number in missing:
        value = sap_po_write_client.get_purchase_order_number(po_number)
        if value:
            store_sap_po_number(db, po_number, value)
    if missing:
        attach_sap_po_numbers(db, items)


def list_po_numbers_missing_custom_number(db, limit: int = 15) -> list:
    """Background loop's worklist (server.py) - distinct PO numbers
    that have never been resolved to a custom SAP PO Number yet, capped
    per cycle so a big backlog catches up gradually instead of one very
    slow cycle. Sep 10 2026, user report: "Created Purchase Orders" page
    showed many blanks even for POs created well after the one-off
    fetch-at-creation was added - SAP's read side has a short propagation
    lag right after a Create, so that immediate fetch can legitimately
    come back empty. Union the vendor PO cache AND recent (last 30 days)
    `purchase_order_creation_history` entries here so those get retried
    automatically until they resolve, instead of staying blank forever.
    Sep 10 2026 follow-up bug: a chunk of OLD POs (from well before this
    feature existed) genuinely never resolve in SAP at all (confirmed
    live - not a bug on our side) and used to permanently occupy the
    entire ascending-sorted cap, starving every NEWER PO from ever being
    retried. Sort by PO number DESCENDING (newest first) so the PO a
    user is actually looking at right now always gets tried before an
    old, likely-permanently-unresolvable one."""
    known = set(list_active_po_numbers(db))
    recent_created = {
        d["po_number"] for d in db["purchase_order_creation_history"].find(
            {"created_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=30)}}, {"po_number": 1}
        )
    }
    known |= recent_created
    already_fetched = set(db[SAP_PO_NUMBER_COLLECTION].distinct("_id"))
    # Sep 10 2026 bugfix: this was calling plain sorted() (ASCENDING),
    # directly contradicting this function's own docstring above and
    # permanently starving newer POs - a brand new PO like 29430 sorts
    # well after hundreds of old, likely-permanently-unresolvable ones
    # and never made it into the capped `limit` worklist. reverse=True
    # actually sorts newest-first as intended.
    return sorted(known - already_fetched, reverse=True)[:limit]


def _generate_doc_code(db) -> str:
    """S000001, S000002, ... - user's explicit ask (Sep 2026): one fixed
    leading letter "S" followed by a purely numeric, always-incrementing
    counter, same pattern as this app's other locally-generated IDs
    (e.g. stock_transfer_service._next_sto_id) - replaces the old fully
    random 6-char alphanumeric code, which wasn't easy to read/say aloud
    and gave no sense of shipment order."""
    counter = db["counters"].find_one_and_update(
        {"_id": "supplier_shipment"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER,
    )
    return f"S{counter['seq']:06d}"


def _resolve_items(db, vendor_code: str, requested_items: list, exclude_doc_code: str = None) -> list:
    """Shared validation for both create_shipment and
    update_shipment_items - each requested item now carries its OWN
    po_number (a shipment can span multiple POs, user's explicit ask).
    `exclude_doc_code` lets an in-progress edit recompute "remaining"
    without double-counting the shipment's own existing reservation."""
    if not requested_items:
        raise ShipmentValidationError("A shipment must contain at least one item")
    resolved = []
    for req in requested_items:
        po_number = (req.get("po_number") or "").strip()
        item_number = req.get("item_number")
        if not po_number:
            raise ShipmentValidationError(f"Missing PO number for item {item_number}")
        try:
            ship_qty = float(req.get("ship_qty") or 0)
        except (TypeError, ValueError):
            raise ShipmentValidationError(f"Invalid ship quantity for item {item_number} on PO {po_number}")
        cached = db[PO_CACHE_COLLECTION].find_one({"vendor_code": vendor_code, "po_number": po_number, "item_number": item_number, "expired": {"$ne": True}})
        if not cached:
            raise ShipmentValidationError(f"Item {item_number} was not found on Purchase Order {po_number}")
        # Sep 13 2026, user's explicit ask ("why did this delivery come in
        # when PO is not valid") - a supplier could previously create a
        # shipment against a PO line whose cached product_id is missing
        # (a pre-existing SAP product-master link gap on that line, see
        # sap_playwright_supplier_pgr_service.py's missing_products fix),
        # only discovering it much later at GRN time when SAP itself
        # rejects/skips that line. Blocked here at the source instead.
        if not cached.get("product_id"):
            raise ShipmentValidationError(f"Item {item_number} on PO {po_number} cannot be shipped - its Product ID is missing in SAP (ask your buyer/SAP Admin to fix this PO line's product master link)")
        if ship_qty <= 0:
            raise ShipmentValidationError(f"Ship quantity for item {item_number} on PO {po_number} must be greater than 0")
        already_shipped = _shipped_qty_so_far(db, vendor_code, po_number, item_number, exclude_doc_code=exclude_doc_code)
        # Sep 9 2026 fix: was `sap_cached["open_qty"] if sap_cached else
        # (po_qty - already_shipped)` - whenever SAP data was cached
        # (the common case), this checked ONLY against SAP's own Open
        # Qty and completely ignored the vendor's own already-in-transit
        # (not yet SAP-received) shipments for this same item, letting
        # two overlapping in-transit shipments together exceed the PO's
        # real open quantity. _compute_qty_state now always nets out
        # in-transit qty on top of whichever "received" figure is best
        # available (SAP's if read, else this app's own locally-approved
        # total) - see its docstring.
        remaining = _compute_qty_state(db, vendor_code, po_number, item_number, cached.get("po_qty") or 0, exclude_doc_code=exclude_doc_code)["remaining_qty"]
        if ship_qty > remaining + 1e-6:
            raise ShipmentValidationError(f"Item {item_number} on PO {po_number}: cannot ship {ship_qty} - only {remaining:g} still open")
        resolved.append({
            "po_number": po_number,
            "item_number": item_number,
            "product_id": cached.get("product_id"),
            "description": cached.get("description"),
            "po_qty": cached.get("po_qty"),
            "already_shipped_qty": already_shipped,
            "ship_qty": ship_qty,
            "unit_of_measure": cached.get("unit_of_measure"),
            # Buyer entity (RI/RT) of this line's PO - carried onto the shipment
            # itself (Sep 2026, "mrp vendor side changes.docx") so the GRN
            # Approval screen can constrain/pre-fix its Site field without a
            # second SAP round-trip. Not present on shipments created before
            # this field existed.
            "buyer_code": cached.get("buyer_code"),
        })
        # NOTE: `ship_to_site_id` deliberately NOT copied onto the shipment
        # item here - see derive_ship_to_site_id() below, which re-reads it
        # live from PO_CACHE_COLLECTION at GRN lookup time instead. That way
        # a shipment created before this field existed still resolves to the
        # exact site once the PO cache itself has been refreshed.
    buyer_codes = {r["buyer_code"] for r in resolved if r.get("buyer_code")}
    if len(buyer_codes) > 1:
        names = ", ".join(sorted(sap_po_client.buyer_entity_name(c) for c in buyer_codes))
        raise ShipmentValidationError(
            f"A shipment cannot mix Purchase Orders from more than one entity ({names}) - "
            f"create separate shipments per entity"
        )
    return resolved


def shipment_buyer_code(doc: dict) -> str:
    """The buying entity (RI/RT) of a shipment's PO(s), taken from its own
    cached line items. Every item in a shipment now shares one entity since
    the Supplier Dashboard forces a vendor to pick a single entity before
    adding anything to their cart (Sep 2026) - returns None for shipments
    created before this field existed."""
    for it in doc.get("items", []):
        if it.get("buyer_code"):
            return it["buyer_code"]
    return None


def derive_ship_to_site_id(db, doc: dict) -> str:
    """The exact real SAP ship-to Site for this shipment (Sep 2 2026 fix,
    user report: "why is SITE not fixed in GRN?" - buyer_code (RI/RT)
    alone can't pin one site since an entity owns MULTIPLE sites, e.g.
    RI = P1 AND P8, so the old entity-only narrowing left GRN's Site
    field ambiguous between 2 choices, never auto-locked. Looked up
    LIVE from PO_CACHE_COLLECTION (not stored on the shipment itself) so
    even a shipment created before `ship_to_site_id` existed resolves
    correctly once its PO's cache row has been refreshed at least once
    since this fix shipped. Returns None (falls back to the coarser
    entity-level allowed_site_ids_for_buyer_code below) if any line's
    site is still unknown or the shipment's items span more than one
    site."""
    site_ids = set()
    for it in doc.get("items", []):
        cached = db[PO_CACHE_COLLECTION].find_one(
            {"vendor_code": doc["vendor_code"], "po_number": it["po_number"], "item_number": it["item_number"]},
            {"ship_to_site_id": 1},
        )
        if not cached or not cached.get("ship_to_site_id"):
            return None
        site_ids.add(cached["ship_to_site_id"])
    return site_ids.pop() if len(site_ids) == 1 else None


def allowed_site_ids_for_buyer_code(db, buyer_code: str) -> list:
    """Real physical sites belonging to a buying entity (RI/RT) - keeps the
    GRN Approval screen's Site field from letting an RI PO be received under
    an RT site or vice versa ("RI and RT Site should be non-editable and
    pre-fixed based on shipment code", mrp vendor side changes.docx). Falls
    back to every known site when the entity can't be determined (legacy
    shipments)."""
    all_sites = inventory_service.list_known_sites(db)
    if not buyer_code:
        return all_sites
    return [s for s in all_sites if company_and_set_of_books_for_site(s)[0] == buyer_code]


def create_shipment(db, account: dict, requested_items: list, vendor_code: str = None, created_on_behalf_by: str = None) -> dict:
    vendor_code = vendor_code or account["vendor_code"]
    resolved_items = _resolve_items(db, vendor_code, requested_items)
    doc_code = _generate_doc_code(db)
    now = datetime.now(timezone.utc)
    doc = {
        "_id": doc_code,
        "account_id": account["_id"],
        "vendor_code": vendor_code,
        "company_name": account["company_name"],
        "items": resolved_items,
        "status": "in_transit",
        # Sep 18 2026, user's explicit ask: shipments created by internal
        # staff via the "Act as Supplier" page look identical to a
        # supplier-created one everywhere else, but carry this flag for
        # traceability (shown on the GRN Approval + Supplier Shipments
        # screens) - None for real supplier-created shipments.
        "created_on_behalf_by": created_on_behalf_by,
        "sap_sync_status": "not_applicable",
        "sap_gr_result": None,
        "sap_movement_status": "not_applicable",
        "sap_movement_result": None,
        "supplier_doc_num": None,
        "site_id": None,
        "warehouse_id": None,
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "approved_by": None,
        "rejected_at": None,
        "rejected_by": None,
        "rejection_reason": None,
        "discrepancy_reason": None,
        "discrepancy_items": None,
        "discrepancy_marked_by": None,
        "discrepancy_marked_at": None,
    }
    db[SHIPMENTS_COLLECTION].insert_one(doc)
    return doc


def update_shipment_items(db, account: dict, doc_code: str, requested_items: list) -> dict:
    """Lets a vendor add/remove/change quantities on their OWN shipment
    for as long as it's still `in_transit` OR `discrepancy` - locked the
    moment it's Approved or Rejected (user's explicit ask, Aug 28 2026).
    Editing a `discrepancy` shipment automatically clears the flag and
    puts it back to `in_transit` for staff to re-review (that's the
    whole point of the discrepancy note - "fix it and it comes back").
    Scoped by the SHIPMENT's own vendor_code (set at creation, possibly
    under a testing-mode impersonation - see create_shipment above), not
    the account's own vendor_code, so editing stays consistent
    regardless of which vendor was being viewed when it was created."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["account_id"] != account["_id"]:
        raise ShipmentNotFoundError("No shipment found for this code")
    if doc["status"] not in ("in_transit", "discrepancy"):
        raise ShipmentValidationError("This shipment has already been processed and can no longer be changed")
    resolved_items = _resolve_items(db, doc["vendor_code"], requested_items, exclude_doc_code=doc_code)
    update = {"items": resolved_items, "updated_at": datetime.now(timezone.utc)}
    if doc["status"] == "discrepancy":
        update.update({
            "status": "in_transit", "discrepancy_reason": None, "discrepancy_items": None,
            "discrepancy_marked_by": None, "discrepancy_marked_at": None,
        })
    db[SHIPMENTS_COLLECTION].update_one({"_id": doc["_id"]}, {"$set": update})
    return get_shipment_by_code(db, doc_code)


def list_shipments_for_vendor(db, vendor_code: str) -> list:
    return list(db[SHIPMENTS_COLLECTION].find({"vendor_code": vendor_code}).sort("created_at", -1))


def _with_display_notification_ids(doc: dict) -> dict:
    """Sep 2026, user's explicit ask ("add a column...so user can
    easily find in SAP") - every approved shipment's real SAP reference
    is this exact composite ID (see _build_notification_id) whether it
    went through the manual-notification path (which already stores
    it) or the auto/Playwright path (which builds and uses the
    identical ID inline but never persists it) - computed here on
    read, display-only, so the GRN screen can show it either way."""
    if doc.get("status") == "approved" and not doc.get("manual_gr_notification_ids"):
        doc["manual_gr_notification_ids"] = {
            po: _build_notification_id(doc.get("supplier_doc_num"), doc["_id"], po)
            for po in {it["po_number"] for it in doc.get("items", [])}
        }
    return doc


def list_shipments(db, status: str = None) -> list:
    query = {"status": status} if status else {}
    return [_with_display_notification_ids(doc) for doc in db[SHIPMENTS_COLLECTION].find(query).sort("created_at", -1)]


def get_shipment_by_code(db, doc_code: str) -> dict:
    doc = db[SHIPMENTS_COLLECTION].find_one({"_id": (doc_code or "").strip().upper()})
    if not doc:
        raise ShipmentNotFoundError("No shipment found for this code")
    buyer_code = shipment_buyer_code(doc)
    doc["buyer_code"] = buyer_code
    doc["buyer_entity_name"] = sap_po_client.buyer_entity_name(buyer_code) if buyer_code else None
    return _with_display_notification_ids(doc)


def reject_shipment(db, doc_code: str, rejected_by: str, reason: str) -> dict:
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] not in ("in_transit", "discrepancy"):
        raise ShipmentValidationError(f"Shipment is already {doc['status']}")
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "status": "rejected", "rejected_at": datetime.now(timezone.utc),
            "rejected_by": rejected_by, "rejection_reason": (reason or "").strip() or None,
        }},
    )
    return get_shipment_by_code(db, doc_code)


def mark_discrepancy(db, doc_code: str, marked_by: str, reason: str, items: list) -> dict:
    """Store staff (not the vendor) flag a physical mismatch - free-text
    reason + the specific line item(s) that don't match. No SAP write
    happens while a shipment sits in this state; the expected fix path
    is the vendor editing their own shipment (see update_shipment_items),
    which auto-clears this and puts it back to in_transit."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "in_transit":
        raise ShipmentValidationError(f"Shipment is already {doc['status']}")
    reason = (reason or "").strip()
    if not reason:
        raise ShipmentValidationError("A discrepancy reason is required")
    if not items:
        raise ShipmentValidationError("Select at least one mismatched line item")
    valid_keys = {(it["po_number"], it["item_number"]) for it in doc["items"]}
    flagged = []
    for it in items:
        key = (it.get("po_number"), it.get("item_number"))
        if key not in valid_keys:
            raise ShipmentValidationError(f"Item {it.get('item_number')} on PO {it.get('po_number')} is not part of this shipment")
        flagged.append({"po_number": key[0], "item_number": key[1]})
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "status": "discrepancy", "discrepancy_reason": reason, "discrepancy_items": flagged,
            "discrepancy_marked_by": marked_by, "discrepancy_marked_at": datetime.now(timezone.utc),
        }},
    )
    return get_shipment_by_code(db, doc_code)


STOCK_STATUS_LABEL_TO_CODE = {"Inspection": "1", "Not Assigned": ""}


def _resolve_source_stock_status(inventory_client, site_id: str, source_area_id: str, product_id: str, qty: float) -> str:
    """Sep 2 2026 fix (real root cause of "Negative stock not permitted
    in logistics area P1-RM" / "No inventory items found" on shipment
    MU7DE2): the Goods Movement call below used to always leave
    InventoryStockStatusCode blank (assuming "Not Assigned"/plain
    stock), but a live check of this material's actual on-hand
    inventory found it sits under "Inspection" status instead (SAP's
    own QM/Inspection Plan routing for this material on PO receipt) -
    the blank-status bucket usually has little to no matching balance,
    so the move was rejected outright. This looks up which status the
    source area's balance for this exact material/qty ACTUALLY sits
    under right now and targets that (a plain relocation keeps the
    same status on both ends - this is not a status-change posting,
    see `_post_goods_movement_for_items`'s docstring). Falls back to
    blank if nothing matches, same as the original behavior."""
    try:
        rows = inventory_client.get_inventory_detail(site_id=site_id, product_ids=[product_id])
    except Exception:
        return ""
    for row in rows:
        if row.get("logistics_area_id", "").rsplit("/", 1)[-1] == source_area_id and row.get("qty", 0) >= qty:
            return STOCK_STATUS_LABEL_TO_CODE.get(row.get("stock_status"), "")
    return ""


def _skipped_line_items_from_gr_results(doc: dict, per_po: list) -> set:
    """Sep 13 2026, user's explicit ask - a PO can now come back "posted"
    while still carrying `skipped_items` for the specific line(s) whose
    Goods Receipt was dropped (missing product_id, see
    sap_playwright_supplier_pgr_service.py's _post_one_po). Those exact
    lines never actually got received in SAP, so step 2 (Goods Movement)
    below must not try to move stock for them - returns the
    (po_number, item_number) pairs to exclude.

    Sep 14 2026 fix (real incident, shipment AB54TT: PO 29533/29534 came
    back with a WHOLE-PO status="skipped", e.g. because the PO was
    Cancelled in SAP - not merely a partial per-line skip within an
    otherwise-posted PO) - every line belonging to a non-"posted" PO
    result must ALSO be excluded, or this would try to move stock for
    material that was never actually received in SAP at all."""
    excluded = {(r.get("po_number"), s.get("item_number")) for r in (per_po or []) for s in (r.get("skipped_items") or [])}
    non_posted_pos = {r.get("po_number") for r in (per_po or []) if r.get("status") != "posted"}
    excluded.update((it["po_number"], it["item_number"]) for it in doc.get("items", []) if it["po_number"] in non_posted_pos)
    return excluded


def _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id: str, site_id: str, warehouse_id: str, skipped_line_items: set = None) -> dict:
    """Step 2 of the live SAP write - moves each shipment line's
    received qty into the receiver's chosen warehouse. Source area
    defaults to this app's own "{site}-RM" convention (same one used
    everywhere else for raw-material receiving) - the GSA schema itself
    gives no way to know for certain where SAP landed the Goods Receipt,
    so this is a best-effort default (see sap_gsa_write_client.py module
    docstring); a real SAP-side rejection here is expected to happen and
    be iterated on, not a local bug.

    IMPORTANT, confirmed live Aug 28 2026: passing an explicit
    InventoryStockStatusCode ("1"/Quality Inspection) OR
    InventoryRestrictedUseIndicator=true on the TARGET - EITHER one
    alone - is rejected by this tenant with "No inventory items found
    for external id ..." (verified via a real, immediately-reversed 1 EA
    P2-RM->P2-QC test movement: plain move succeeds, either flag alone
    fails the same way). So this tenant's Goods Movement service, as
    configured today, cannot force RESTRICTED/Quality-Inspection status
    this way - this does a PLAIN move only. Getting real RESTRICTED QI
    stock needs either the product's own SAP Inspection Plan config (so
    the GSA's own automatic receiving flow routes it there) or a
    different SAP service/config - flagged as a known open item, not
    silently claimed as working.

    Sep 2 2026 UPDATE (real root cause of "Negative stock not permitted
    in logistics area P1-RM" / "No inventory items found" on shipment
    MU7DE2): live-checked this exact material's ACTUAL on-hand stock at
    P1-RM via `sap_inventory_client` after a real GR posted - it sits
    under stock_status="Inspection" (SAP's own QM/Inspection Plan
    routing for THIS material on receipt), not "Not Assigned"/blank.
    The Aug 28 finding above is still correct in general (you can't use
    this API to CHANGE a stock's status), but it doesn't apply here -
    this isn't a status change, it's a plain relocation of stock that
    is ALREADY at Inspection status, so the movement's OWN status code
    must match reality (Inspection, "1") on both ends, not be left
    blank/"Not Assigned" (which usually has little to no matching
    balance there, hence the negative-stock/no-items-found errors)."""
    per_item = []
    all_ok = True
    skipped_line_items = skipped_line_items or set()
    for it in doc["items"]:
        if (it["po_number"], it["item_number"]) in skipped_line_items:
            per_item.append({"po_number": it["po_number"], "item_number": it["item_number"], "product_id": it["product_id"], "ok": True, "skipped": True, "note": "Goods Receipt for this line was skipped in SAP (missing Product ID) - no stock to move"})
            continue
        qty = it.get("actual_qty", it["ship_qty"])
        # Sep 17 2026 bug fix (real user report + SAP screenshot proof,
        # shipment S7KKXU/delivery 53599: SAP's own "Inbound Warehouse
        # Order Overview" showed Target Logistics Area ID = P8-HOLD for
        # the Put Away, NOT P8-RM): Site P8's Material Flow Destination
        # rule routes EVERY inbound Goods Receipt into the neutral
        # {SITE}-HOLD staging warehouse regardless of warehouse_id chosen
        # at approval time - the exact same quirk already documented/
        # worked around for inbound STOs (see inbound_receipt_service.py's
        # _receipt_hold_warehouse_id).
        #
        # Sep 18 2026, generalized from P8-only to every site (real
        # incident, GRN S000001/PO 29703 at Site P3: SAP's own Inbound
        # Warehouse Order Overview + Stock Overview screenshots showed
        # BOTH line items - 13INTIEBELT/IRON-SCR - landed in P3-HOLD under
        # Quality Inspection status, not P3-RM as this used to assume for
        # every non-P8 site. Assuming the wrong warehouse meant
        # _resolve_source_stock_status below found zero matching stock
        # there and the movement was attempted from the wrong (empty)
        # area entirely - "No inventory items found"/"You cannot carry
        # out goods movements involving this logistics area" from SAP).
        # User confirmed live every site now has its own "{SITE}-HOLD"
        # staging warehouse for this same reason - no hardcoded site
        # check needed anymore.
        source_area = f"{site_id}-HOLD"
        # Sep 12 2026 bug fix (real incident, shipment LFG29A/PO 29482 -
        # user's explicit report "after success grn why an error
        # occurred": "SAP rejected the movement: Source and target
        # logistics area are same"): this app's own default RM warehouse
        # for most sites IS literally "{site}-RM" (see the site-specific
        # default warehouse feature) - the exact same string this
        # function assumes the Goods Receipt already landed in. When the
        # shipment's chosen target warehouse happens to be that same
        # default, source == target and SAP correctly rejects the
        # movement as a no-op. There's genuinely nothing to move in that
        # case - the stock is already exactly where it needs to be.
        if source_area == warehouse_id:
            per_item.append({"po_number": it["po_number"], "item_number": it["item_number"], "product_id": it["product_id"], "ok": True, "skipped": True, "note": f"Already in {warehouse_id} on receipt - no movement needed"})
            continue
        stock_status = _resolve_source_stock_status(inventory_client, site_id, source_area, it["product_id"], qty)
        try:
            result = goods_movement_client.goods_movement(
                owner_party_id=owner_party_id, product_id=it["product_id"],
                source_logistics_area_id=source_area, target_logistics_area_id=warehouse_id,
                quantity=qty, quantity_uom=it.get("unit_of_measure") or "EA", site_id=site_id,
                dry_run=False, target_stock_status_code=stock_status,
            )
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        if not result.get("ok"):
            all_ok = False
        per_item.append({"po_number": it["po_number"], "item_number": it["item_number"], "product_id": it["product_id"], **result})
    return {"ok": all_ok, "per_item": per_item}


def group_items_by_po_for_gr(doc: dict) -> dict:
    """Shapes a shipment's items into the {po_number: {supplier_doc_num,
    bill_date, vendor_code, item_qtys, item_products, item_uoms}} form
    sap_playwright_supplier_pgr_service.post_goods_receipt_via_ui
    expects - one entry per distinct PO, grouping every line item of
    that PO (user's explicit ask). `item_products`/`item_uoms` let the
    hybrid flow's SOAP create step (Sep 12 2026) reference each PO line
    directly and set its real Delivery Quantity, and let the Playwright
    side match each grid row by Product ID rather than trusting row
    order (see that module's docstring)."""
    grouped = {}
    for it in doc["items"]:
        po = grouped.setdefault(it["po_number"], {
            "doc_code": doc.get("_id"),
            "supplier_doc_num": doc.get("supplier_doc_num"),
            "bill_date": doc.get("bill_date"),
            "vendor_code": doc.get("vendor_code"),
            "item_qtys": {},
            "item_products": {},
            "item_uoms": {},
        })
        po["item_qtys"][it["item_number"]] = it.get("actual_qty", it["ship_qty"])
        po["item_products"][it["item_number"]] = it["product_id"]
        po["item_uoms"][it["item_number"]] = it.get("unit_of_measure")
    return grouped


def prepare_approval(db, doc_code: str, approved_by: str, approved_by_user_id: str, supplier_doc_num: str, bill_date: str, site_id: str,
                      warehouse_id: str, item_actual_qtys: dict, grn_mode: str = "auto") -> dict:
    """Records the internal approval unconditionally (staff have already
    physically matched goods + invoice) - sync/fast, the live SAP write
    itself (Playwright, slow) happens as a background job kicked off by
    the caller right after this returns (see server.py's approve
    endpoint). `item_actual_qtys`: {(po_number, item_number): qty} - the
    staff-CONFIRMED received quantity, defaults to the vendor's own
    claimed ship_qty for any line not explicitly overridden.

    Sep 17 2026, Manual GRN feature: `grn_mode` ("auto"|"manual") is the
    approver's own user-level preference at the moment they clicked
    Approve (see auth_users.manual_grn_preference) - stored on the
    shipment so every later step (job dispatch, retry, the GRN Approval
    screen's own UI) treats this exact shipment consistently even if the
    approver later flips their own preference. `approved_by_user_id` is
    the approver's auth_users `_id` - needed later so a quantity mismatch
    found in `check_manual_gr_quantities` can block THIS specific person
    (not just whoever happens to click "Re-check SAP")."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] not in ("in_transit", "discrepancy"):
        raise ShipmentValidationError(f"Shipment is already {doc['status']}")
    allowed_sites = allowed_site_ids_for_buyer_code(db, doc.get("buyer_code"))
    if allowed_sites and site_id not in allowed_sites:
        entity_name = sap_po_client.buyer_entity_name(doc.get("buyer_code"))
        raise ShipmentValidationError(
            f"This shipment's PO belongs to {entity_name} - Site must be one of: {', '.join(allowed_sites)}"
        )
    items = []
    for it in doc["items"]:
        key = (it["po_number"], it["item_number"])
        items.append({**it, "actual_qty": item_actual_qtys.get(key, it["ship_qty"])})
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "items": items, "status": "approved", "approved_at": datetime.now(timezone.utc), "approved_by": approved_by,
            "approved_by_user_id": approved_by_user_id, "grn_mode": grn_mode,
            "supplier_doc_num": (supplier_doc_num or "").strip() or None, "bill_date": (bill_date or "").strip() or None,
            "site_id": site_id, "warehouse_id": warehouse_id, "sap_sync_status": "pending", "sap_gr_result": None,
            "sap_movement_status": "not_applicable", "sap_movement_result": None,
            "manual_gr_notification_ids": None, "manual_gr_mismatch_items": None,
        }},
    )
    return get_shipment_by_code(db, doc_code)


def finalize_goods_receipt(db, doc_code: str, gr_results: list, goods_movement_client, inventory_client, owner_party_id: str, sap_username: str = None) -> dict:
    """Called after sap_playwright_supplier_pgr_service.
    post_goods_receipt_via_ui returns - `gr_results` is its
    results list. All POs in the shipment must have posted for step 2
    (Goods Movement) to run, matching approve_shipment's old
    all-or-nothing behaviour.

    Sep 12 2026, user's explicit ask ("which user you used while
    receiving the recent grn... in SAP") - `sap_username` (whichever of
    the pooled SAP UI logins actually ran this specific attempt, see
    playwright_concurrency.py) is now stamped onto every attempt so this
    is always answerable from the shipment's own data going forward,
    without needing to dig through backend logs after the fact.

    Sep 10 2026, user's explicit ask ("do not show status received
    until SAP inbound number is received" + "retry... not work...you
    have to resolve this"): a Playwright-level failure used to leave
    `sap_sync_status` as "pending" forever (same real SAP validation
    error recurring on every Retry click, since Retry re-attempts the
    identical action against the identical unresolved SAP-side issue) -
    indistinguishable in the UI from "still actively trying". After
    `MAX_GR_RETRIES_BEFORE_FAILED` unsuccessful retries, flip to a
    distinct "failed" status so staff see this needs manual SAP
    attention instead of waiting on a Retry button that will keep
    reproducing the same error."""
    doc = get_shipment_by_code(db, doc_code)
    all_ok = bool(gr_results) and all(r.get("status") == "posted" for r in gr_results)
    all_skipped = bool(gr_results) and all(r.get("status") == "skipped" for r in gr_results)
    # Sep 14 2026 fix (real incident, shipment AB54TT: 1 of 3 POs posted,
    # the other 2 permanently skipped because they were Cancelled in
    # SAP mid-flight) - a mix of posted+skipped, with nothing left that
    # a Retry could ever fix, used to fall through to "pending" forever
    # (or eventually "failed" after MAX_GR_RETRIES_BEFORE_FAILED) even
    # though it's genuinely done - the skipped PO(s) will NEVER succeed
    # on retry (SAP's Cancelled state doesn't revert), and the posted
    # one already has real stock to move. New "partial" status is a
    # distinct, terminal, no-more-retry-needed outcome.
    sap_sync_status = _compute_sap_sync_status(gr_results, doc.get("sap_gr_retry_count", 0))
    sap_gr_result = {"ok": all_ok, "per_po": gr_results, "sap_username": sap_username}

    sap_movement_status = "not_applicable"
    sap_movement_result = None
    if sap_sync_status in ("posted", "partial"):
        skipped_line_items = _skipped_line_items_from_gr_results(doc, gr_results)
        sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id, doc["site_id"], doc["warehouse_id"], skipped_line_items)
        sap_movement_status = "posted" if sap_movement_result.get("ok") else "pending"
    else:
        sap_movement_result = {"ok": False, "reason": "Skipped - Goods Receipt (step 1) did not succeed yet"}

    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "sap_sync_status": sap_sync_status, "sap_gr_result": sap_gr_result,
            "sap_movement_status": sap_movement_status, "sap_movement_result": sap_movement_result,
        }},
    )
    return get_shipment_by_code(db, doc_code)


MAX_GR_RETRIES_BEFORE_FAILED = 3


def _compute_sap_sync_status(gr_results: list, retry_count: int) -> str:
    all_ok = bool(gr_results) and all(r.get("status") == "posted" for r in gr_results)
    all_skipped = bool(gr_results) and all(r.get("status") == "skipped" for r in gr_results)
    all_settled = bool(gr_results) and all(r.get("status") in ("posted", "skipped") for r in gr_results)
    any_posted = any(r.get("status") == "posted" for r in gr_results)
    if all_ok:
        return "posted"
    if all_skipped:
        return "skipped"
    if all_settled and any_posted:
        return "partial"
    if retry_count >= MAX_GR_RETRIES_BEFORE_FAILED:
        return "failed"
    return "pending"


def finalize_manual_notification(db, doc_code: str, results: list) -> dict:
    """Sep 17 2026, Manual GRN (No-Playwright) path - called after
    sap_playwright_supplier_pgr_service.create_inbound_delivery_notifications_only
    returns. Unlike the auto/Playwright path, this is NOT a terminal
    outcome - it just means staff can now go post the actual Goods
    Receipt themselves in SAP using these exact notification IDs.
    `sap_sync_status="awaiting_manual_gr"` is what unlocks the "Re-check
    SAP" button on the GRN Approval screen (see check_manual_gr_quantities
    below). Only truly `"failed"` when EVERY PO's notification create
    failed - nothing left for staff to even go post in SAP."""
    doc = get_shipment_by_code(db, doc_code)
    notification_ids = {r["po_number"]: r["notification_id"] for r in results if r.get("notification_id")}
    any_created = any(r.get("status") == "notification_created" for r in results)
    sap_sync_status = "awaiting_manual_gr" if any_created else "failed"
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "sap_sync_status": sap_sync_status, "sap_gr_result": {"ok": any_created, "per_po": results},
            "manual_gr_notification_ids": notification_ids,
        }},
    )
    return get_shipment_by_code(db, doc_code)


MISMATCH_QTY_TOLERANCE = 1e-3


def _parse_sap_qty(value) -> float:
    """Sep 16 2026 bug fix (real error, user-triggered "Re-check SAP"
    500'd): SAP's confirmation report returns FCCONF_QUAN as e.g.
    '2.0000000 kg', not a bare number - same "value unit" shape
    sap_po_analytics_client.py's own `_parse_qty` already handles for a
    different report. `float()` on the raw string crashed every single
    Re-check."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text.split()[0].replace(",", ""))
    except ValueError:
        return 0.0


def check_manual_gr_quantities(db, doc_code: str, report_client, goods_movement_client,
                                inventory_client, owner_party_id: str) -> dict:
    """Sep 17 2026, Manual GRN feature - the "Re-check SAP" button.
    Compares SAP's own confirmed quantity (FCCONF_QUAN on the same
    "Inbound Delivery Detailed Details" report already used elsewhere in
    this app, matched on this exact notification_id) against what this
    shipment claims staff confirmed (`actual_qty`) for every line, grouped
    by product_id (never by item_number - a PO can have more than one
    line for the identical product, see sap_playwright_supplier_pgr_
    service.py's own matching-key docstring for why item_number alone
    isn't a safe correlation key against SAP's own product-level report
    rows).

    Sep 16 2026 bug fix (real live-tested mismatch that never cleared,
    user report re: shipment 3B8VR3/delivery 53483): `CPRODUCT_UUID` on
    this report is, DESPITE ITS NAME, just the plain-text Product ID -
    confirmed live (row for PO 29535 showed `CPRODUCT_UUID: "IRON-SCR"`,
    `TPRODUCT_UUID: "IRON SCRAP (72041000)"` - i.e. CPRODUCT_UUID is the
    code, TPRODUCT_UUID is the description, same "misleadingly named"
    pattern this report's own module docstring already documents for
    CDELIVERY_UUID). The original version of this function resolved each
    item's product_id to a REAL SAP material UUID via sap_material_client
    and matched on that instead - which could never match a report row
    keyed by the plain product_id, so every Re-check found `sap_confirmed_
    qty=0` regardless of what was actually posted in SAP. Matching
    directly on product_id (no resolution needed at all) is both simpler
    and the only value that's actually correct here.

    On a full match: reuses `finalize_goods_receipt` (same function the
    auto/Playwright path uses) so this shipment ends up in the exact same
    terminal "posted"/warehouse-movement state either way - one behavior,
    two ways to get the SAP Goods Receipt itself done. Also clears the
    approver's block if one was set by an earlier mismatch on this exact
    shipment.

    On any mismatch: FLAGS every line sharing that mismatched product
    (can't tell which specific line is wrong once >1 line shares a
    product - same limitation, stated plainly) and BLOCKS the original
    approver (`approved_by_user_id`) from approving any further GRN until
    an Admin override clears it or a later Re-check finds SAP corrected."""
    doc = get_shipment_by_code(db, doc_code)
    if doc.get("sap_sync_status") not in ("awaiting_manual_gr", "manual_mismatch"):
        raise ShipmentValidationError("This shipment is not awaiting a manual SAP Goods Receipt")
    notification_ids = doc.get("manual_gr_notification_ids") or {}
    po_numbers = sorted({it["po_number"] for it in doc["items"] if notification_ids.get(it["po_number"])})
    if not po_numbers:
        raise ShipmentValidationError("No Inbound Delivery Notification was ever created in SAP for this shipment - nothing to re-check")
    mismatches = []
    delivery_id_by_po = {}
    for po_number in po_numbers:
        notification_id = notification_ids[po_number]
        try:
            rows = report_client.find_confirmation_rows(po_number, notification_id)
        except Exception as e:
            raise ShipmentValidationError(f"Could not reach SAP to verify PO {po_number}: {e}")
        # Sep 16 2026 bug fix #2 (real live-tested case, user screenshot-
        # confirmed against SAP's own Inbound Deliveries screen for
        # shipment 3B8VR3/PO 29535): this analytics report can carry
        # STALE/orphaned rows from earlier abandoned notification-create
        # attempts sharing the exact same CREF_ID (reference) - SAP's own
        # UI showed exactly ONE real Inbound Delivery (ID 53483) for this
        # shipment, yet the report returned 3 extra older CDELIVERY_UUIDs
        # too. Only the row(s) for the MOST RECENT CDELIVERY_UUID are
        # real/current - summing every row ever extracted under this
        # reference massively over-counts. Also: FCCONF_QUAN itself reads
        # 2x SAP's own displayed "Fulfilled Quantity" on that exact row
        # (confirmed via screenshot: SAP showed "1 kg" Fulfilled Quantity,
        # FCCONF_QUAN said "2.0000000 kg") - FCINV_QUAN matched the real
        # "Fulfilled Quantity" exactly, so that's the field trusted here.
        if rows:
            try:
                latest_delivery_id = max(rows, key=lambda r: int(r.get("CDELIVERY_UUID") or 0)).get("CDELIVERY_UUID")
            except (TypeError, ValueError):
                latest_delivery_id = rows[-1].get("CDELIVERY_UUID")
            rows = [r for r in rows if r.get("CDELIVERY_UUID") == latest_delivery_id]
            delivery_id_by_po[po_number] = latest_delivery_id
        confirmed_by_product = {}
        for r in rows:
            product_key = r.get("CPRODUCT_UUID")
            if product_key:
                confirmed_by_product[product_key] = confirmed_by_product.get(product_key, 0) + _parse_sap_qty(r.get("FCINV_QUAN"))
        shipped_by_product = {}
        for it in doc["items"]:
            if it["po_number"] != po_number:
                continue
            shipped_by_product.setdefault(it["product_id"], []).append(it)
        for product_id, group_items in shipped_by_product.items():
            shipped_qty = sum(it.get("actual_qty", it["ship_qty"]) for it in group_items)
            confirmed_qty = confirmed_by_product.get(product_id, 0.0)
            if not rows or abs(shipped_qty - confirmed_qty) > MISMATCH_QTY_TOLERANCE:
                for it in group_items:
                    mismatches.append({
                        "po_number": po_number, "item_number": it["item_number"], "product_id": it["product_id"],
                        "shipped_qty": shipped_qty, "sap_confirmed_qty": confirmed_qty,
                    })
    approved_by_user_id = doc.get("approved_by_user_id")
    if mismatches:
        db[SHIPMENTS_COLLECTION].update_one(
            {"_id": doc["_id"]}, {"$set": {"sap_sync_status": "manual_mismatch", "manual_gr_mismatch_items": mismatches}},
        )
        if approved_by_user_id:
            db[AUTH_USERS_COLLECTION].update_one(
                {"_id": approved_by_user_id},
                {"$set": {
                    "grn_blocked_shipment": doc_code,
                    "grn_blocked_reason": f"SAP-confirmed quantity does not match the shipped/confirmed quantity on shipment {doc_code} ({len(mismatches)} line(s) flagged)",
                    "grn_blocked_at": datetime.now(timezone.utc),
                }},
            )
        result = get_shipment_by_code(db, doc_code)
        result["recheck_result"] = "mismatch"
        return result
    gr_results = [
        {
            "po_number": po, "status": "posted", "inbound_delivery_id": delivery_id_by_po.get(po),
            "events": [f"Manually confirmed via SAP Re-check (notification {notification_ids[po]}) - quantities matched"],
        }
        for po in po_numbers
    ]
    final = finalize_goods_receipt(db, doc_code, gr_results, goods_movement_client, inventory_client, owner_party_id)
    if approved_by_user_id:
        db[AUTH_USERS_COLLECTION].update_one(
            {"_id": approved_by_user_id, "grn_blocked_shipment": doc_code},
            {"$set": {"grn_blocked_shipment": None, "grn_blocked_reason": None, "grn_blocked_at": None}},
        )
    final["recheck_result"] = "matched"
    return final


def verify_and_correct_gr_status(db, doc_code: str, confirmation_report_client) -> dict:
    """Sep 16 2026, real incident (PO 29284 / shipment WFJEZ2, user's own
    screenshot) - `sap_playwright_supplier_pgr_service.py` now positively
    verifies against SAP's own confirmation report before ever reporting
    "posted" (previously just assumed success whenever no error toast was
    visible - a genuinely silent SAP-side failure got reported as a false
    "Posted" with no Retry button available, permanently stuck). That fix
    only protects FRESH attempts - a shipment already wrongly marked
    posted/partial before it shipped stays stuck with no way back. This
    re-checks every PO on this shipment currently marked "posted" against
    that same (now also credential-fixed - see .env SAP_ODATA_BUSINESS_
    PASSWORD) confirmation report, flips any PO SAP can't actually
    confirm back to "failed" with an explanatory event, and recomputes
    the shipment's overall status via the same rule finalize_goods_receipt
    uses - restoring the Retry button for a genuinely-unposted PO instead
    of leaving it stuck behind a false "Posted"."""
    doc = get_shipment_by_code(db, doc_code)
    gr_result = doc.get("sap_gr_result") or {}
    per_po = gr_result.get("per_po") or []
    corrected = []
    for po in per_po:
        if po.get("status") != "posted":
            continue
        notification_id = _build_notification_id(doc.get("supplier_doc_num"), doc_code, po["po_number"])
        try:
            confirmed_rows = confirmation_report_client.find_confirmation_rows(po["po_number"], notification_id)
        except Exception as e:
            corrected.append({"po_number": po["po_number"], "verify_error": str(e)})
            continue
        if not confirmed_rows:
            po["status"] = "failed"
            po["events"] = (po.get("events") or []) + [
                "Re-verified against SAP (Sep 16 2026 fix) - SAP shows NO confirmed Goods Receipt for "
                "this PO despite it being marked Posted. Corrected to Failed so it can be Retried."
            ]
            corrected.append({"po_number": po["po_number"], "corrected_to": "failed"})
    any_status_change = any(c.get("corrected_to") for c in corrected)
    if any_status_change:
        sap_sync_status = _compute_sap_sync_status(per_po, doc.get("sap_gr_retry_count", 0))
        db[SHIPMENTS_COLLECTION].update_one(
            {"_id": doc["_id"]},
            {"$set": {"sap_gr_result.per_po": per_po, "sap_sync_status": sap_sync_status}},
        )
    updated_doc = get_shipment_by_code(db, doc_code)
    updated_doc["gr_verification_result"] = corrected
    return updated_doc


def reset_gr_retry_count(db, doc_code: str) -> dict:
    """Manual staff override (Sep 10 2026, user's explicit ask) - once
    an SAP Admin confirms whatever caused every prior Retry to hit the
    IDENTICAL SAP error (see finalize_goods_receipt's own docstring) is
    actually fixed on SAP's side, this clears the "failed" escalation so
    the normal Retry button reappears with a fresh
    MAX_GR_RETRIES_BEFORE_FAILED budget. Does NOT itself call SAP - the
    next Retry click does that."""
    doc = get_shipment_by_code(db, doc_code)
    if doc.get("sap_sync_status") != "failed":
        raise ShipmentValidationError("Only a shipment currently marked 'SAP Sync Failed' can have its retry count reset")
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {"sap_sync_status": "pending", "sap_gr_retry_count": 0}},
    )
    return get_shipment_by_code(db, doc_code)


def prepare_retry_goods_receipt(db, doc_code: str) -> dict:
    """Validates a shipment is eligible for a fresh Goods Receipt attempt
    (mirrors the old retry_goods_receipt's own guard) - no SAP call here,
    the caller runs the same Playwright job + finalize_goods_receipt used
    by the initial approval. Sep 10 2026: counts this attempt so
    finalize_goods_receipt can flip to "failed" after enough retries of
    the same unresolved SAP error (see its own docstring)."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "approved":
        raise ShipmentValidationError("This shipment has not been approved yet")
    if doc.get("sap_sync_status") in ("posted", "partial"):
        raise ShipmentValidationError("The Goods Receipt has already posted to SAP (or is permanently partially posted) - nothing left to retry")
    db[SHIPMENTS_COLLECTION].update_one({"_id": doc["_id"]}, {"$inc": {"sap_gr_retry_count": 1}})
    return get_shipment_by_code(db, doc_code)



def retry_goods_movement(db, doc_code: str, goods_movement_client, inventory_client, owner_party_id: str) -> dict:
    """Retries ONLY step 2 (the Goods Movement into the chosen warehouse)
    for a shipment whose Goods Receipt (step 1) already posted but the
    movement itself failed/is still pending - mirrors the retry-wip-
    clearing pattern already used elsewhere in this app."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "approved":
        raise ShipmentValidationError("This shipment has not been approved yet")
    if doc.get("sap_sync_status") not in ("posted", "partial"):
        raise ShipmentValidationError("The Goods Receipt (step 1) has not posted to SAP yet - nothing to retry")
    if not doc.get("site_id") or not doc.get("warehouse_id"):
        raise ShipmentValidationError("This shipment has no warehouse recorded to retry into")
    skipped_line_items = _skipped_line_items_from_gr_results(doc, (doc.get("sap_gr_result") or {}).get("per_po"))
    sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id, doc["site_id"], doc["warehouse_id"], skipped_line_items)
    sap_movement_status = "posted" if sap_movement_result.get("ok") else "pending"
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {"sap_movement_status": sap_movement_status, "sap_movement_result": sap_movement_result}},
    )
    return get_shipment_by_code(db, doc_code)


def fetch_inbound_delivery_ids_from_sap(doc: dict, report_client) -> dict:
    """Sep 11 2026, user's explicit ask - live-matches a shipment
    against SAP's "Inbound Delivery Detailed Details" analytics report
    to recover a real Inbound Delivery ID our own Playwright automation
    never captured (see sap_inbound_delivery_report_client.py's
    docstring for the full field investigation). Matches per distinct
    PO on the shipment by PO number + the EXACT supplier bill number on
    file, cross-checked against the shipment's own item codes for that
    PO (a bill number typo/reuse could otherwise silently match the
    wrong delivery). Returns {"found": {po_number: inbound_delivery_id},
    "errors": [str, ...]} - never raises for a single PO's no-match/
    ambiguous case, so a multi-PO shipment can still get partial results.

    Bug fix (real user report, Sep 2026, shipment S7KKXU/PO 29685 -
    "having issue while fetching inbound number"): this always searched
    by the bare `supplier_doc_num` alone, but that's never the actual
    reference SAP has on file for a Delivery Notification/GR posted
    under either the manual-notification path OR the current auto/
    Playwright path (both build and submit the composite
    `_build_notification_id` string, e.g. "Test/2535/235-S7KKXU-29685" -
    see that function's own docstring) - so a bare-bill-number search
    could never match, even after the real SAP-side posting was done.
    Now tries the real composite reference first, falling back to the
    bare bill number for any shipment created before that convention
    existed."""
    supplier_doc_num = (doc.get("supplier_doc_num") or "").strip()
    if not supplier_doc_num:
        raise ShipmentValidationError("This shipment has no supplier bill number on file to match against SAP")
    po_numbers = sorted({it["po_number"] for it in doc["items"]})
    found, errors = {}, []
    for po_number in po_numbers:
        product_ids = {it["product_id"] for it in doc["items"] if it["po_number"] == po_number}
        reference = _build_notification_id(supplier_doc_num, doc["_id"], po_number)
        try:
            rows = report_client.find_confirmation_rows(po_number, reference)
            if not rows:
                rows = report_client.find_confirmation_rows(po_number, supplier_doc_num)
        except Exception as e:
            errors.append(f"PO {po_number}: SAP query failed - {e}")
            continue
        matched_rows = [r for r in rows if r.get("CPRODUCT_UUID") in product_ids]
        delivery_ids = {r["CDELIVERY_UUID"] for r in matched_rows if r.get("CDELIVERY_UUID")}
        if not delivery_ids:
            errors.append(f"PO {po_number}: no matching confirmation found in SAP yet for bill '{supplier_doc_num}' (checked reference '{reference}' too)")
        else:
            # Sep 16 2026 bug fix (real user report: "once I fetch from
            # SAP, the Inbound Delivery # should fill" - it wasn't).
            # Root cause: this report can carry STALE/orphaned rows from
            # earlier abandoned notification-create attempts sharing the
            # exact same bill number (confirmed live on a different
            # shipment this session, see check_manual_gr_quantities's own
            # Sep 16 2026 fix) - `len(delivery_ids) > 1` used to treat
            # that as "ambiguous" and refuse to fill anything at all. The
            # highest/most recent CDELIVERY_UUID is the real, current one
            # (SAP assigns these sequentially) - same "latest wins"
            # resolution already applied for Manual GRN Re-check.
            try:
                found[po_number] = max(delivery_ids, key=lambda d: int(d))
            except (TypeError, ValueError):
                found[po_number] = sorted(delivery_ids)[-1]
    return {"found": found, "errors": errors}


def manually_confirm_inbound_delivery(db, doc_code: str, po_number: str, inbound_delivery_id: str, confirmed_by: str,
                                       goods_movement_client=None, inventory_client=None, owner_party_id: str = None) -> dict:
    """Applies a staff-triggered SAP-confirmed Inbound Delivery ID onto
    one PO's entry in the shipment's sap_gr_result.per_po (see
    fetch_inbound_delivery_ids_from_sap) - flips that PO's own status to
    "posted" and tags it `manually_confirmed` (kept distinct from an
    automated Playwright success everywhere in the UI). Only flips the
    shipment's OVERALL sap_sync_status to "posted" once every PO on the
    shipment has posted (mirrors finalize_goods_receipt's all-or-nothing
    rule) - a multi-PO shipment with one PO still failed stays as-is.

    Bug fix (user's explicit ask, Sep 2026: "if fetching success then
    move it to the RM warehouse") - this used to only ever update
    sap_gr_result/sap_sync_status and left step 2 (the actual Goods
    Movement into the shipment's chosen warehouse) undone forever, since
    unlike finalize_goods_receipt/check_manual_gr_quantities (the other
    two ways a GR can be confirmed) it never called
    _post_goods_movement_for_items at all. Now runs it once every PO on
    the shipment is confirmed posted (same all-or-nothing timing as
    those two), skipped if it already ran (sap_movement_status=="posted")
    so re-fetching an already-fully-confirmed shipment can't double-move
    stock."""
    doc = get_shipment_by_code(db, doc_code)
    per_po = list((doc.get("sap_gr_result") or {}).get("per_po") or [])
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "po_number": po_number, "status": "posted", "inbound_delivery_id": inbound_delivery_id,
        "manually_confirmed": True, "manually_confirmed_by": confirmed_by, "manually_confirmed_at": now,
    }
    replaced = False
    for i, p in enumerate(per_po):
        if p.get("po_number") == po_number:
            per_po[i] = {**p, **entry}
            replaced = True
    if not replaced:
        per_po.append(entry)
    all_ok = bool(per_po) and all(p.get("status") == "posted" for p in per_po)
    sap_sync_status = "posted" if all_ok else doc.get("sap_sync_status")
    update = {"sap_gr_result": {"ok": all_ok, "per_po": per_po}, "sap_sync_status": sap_sync_status}
    if all_ok and doc.get("sap_movement_status") != "posted" and goods_movement_client and doc.get("site_id") and doc.get("warehouse_id"):
        skipped_line_items = _skipped_line_items_from_gr_results(doc, per_po)
        sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id, doc["site_id"], doc["warehouse_id"], skipped_line_items)
        update["sap_movement_status"] = "posted" if sap_movement_result.get("ok") else "pending"
        update["sap_movement_result"] = sap_movement_result
    db[SHIPMENTS_COLLECTION].update_one({"_id": doc["_id"]}, {"$set": update})
    return get_shipment_by_code(db, doc_code)


def mark_put_away_confirmed(db, doc_code: str, po_number: str, confirmed: bool, events: list) -> None:
    """Sep 19 2026 - patches just `put_away_confirmed`/`events` onto one
    PO's own `sap_gr_result.per_po` entry (never touches status or
    `inbound_delivery_id`) - set by the full-auto GRN's background Put
    Away confirmation retry (see server.py's `_auto_finish_full_auto_grn`,
    sap_playwright_supplier_pgr_service._confirm_put_away_task's
    docstring for the real bug this closes out: Fulfilled Quantity
    staying 0 in SAP after a "Test Full Automated GRN" run)."""
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": (doc_code or "").strip().upper(), "sap_gr_result.per_po.po_number": po_number},
        {"$set": {"sap_gr_result.per_po.$.put_away_confirmed": confirmed, "sap_gr_result.per_po.$.events": events}},
    )
