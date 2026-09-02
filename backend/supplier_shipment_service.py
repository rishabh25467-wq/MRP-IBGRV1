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
import secrets
import string
from datetime import datetime, timedelta, timezone

import inventory_service
import sap_po_client
from sap_wip_clearing_client import company_and_set_of_books_for_site

PO_CACHE_COLLECTION = "supplier_portal_po_cache"
SAP_OPEN_QTY_COLLECTION = "sap_po_open_qty_cache"
SHIPMENTS_COLLECTION = "supplier_portal_shipments"
DOC_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")
DOC_CODE_LENGTH = 6

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


def refresh_po_cache(db, vendor_code: str, items: list) -> None:
    """Called every time a LIVE SAP PO fetch succeeds - keeps a durable
    local copy so the vendor's open-PO list (and shipment creation) still
    works between SAP calls, and so a vendor always sees "last known"
    data instead of a hard error the moment SAP itself is briefly down.

    Also EXPIRES any of this vendor's previously-cached rows that are no
    longer in the fresh live fetch (PO now Finished, or fell out of
    sap_po_client's current-window fetch) - a live fetch is the source
    of truth, so a stale/no-longer-open row must not linger forever
    (found by user report Aug 28 2026: the cache kept showing PO rows
    from before the sap_po_client recency-window fix even after that
    fix landed, since nothing had ever deleted them).

    A missing row is only MARKED (`missing_since`) on its first miss,
    and only actually deleted once it's been missing for
    STALE_CYCLES_BEFORE_DELETE consecutive refreshes - protects against
    a single fetch cycle's own window/timing hiccup being mistaken for
    "PO closed" and permanently wiping a still-open PO (real data-loss
    risk flagged by a deployment scan, fixed 2026-08-28: this used to
    hard-delete on the very first miss). A row that reappears in a later
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
    db[PO_CACHE_COLLECTION].update_many({**missing_query, "missing_since": None}, {"$set": {"missing_since": now}})
    stale_cutoff = now - timedelta(minutes=STALE_CYCLES_BEFORE_DELETE * REFRESH_INTERVAL_MINUTES)
    # Soft-delete only (deployment-scan-flagged, fixed 2026-08-29): an unattended background
    # loop must never hard-delete rows outright, even ones scoped this narrowly - mark them
    # `expired` instead so a vendor's PO disappears from their open-PO list (see the
    # `expired` filters in get_cached_pos_with_remaining/_resolve_items below) without losing
    # the underlying record for audit/debugging.
    db[PO_CACHE_COLLECTION].update_many(
        {**missing_query, "missing_since": {"$lte": stale_cutoff}},
        {"$set": {"expired": True, "expired_at": now}},
    )


def refresh_all_vendor_caches(db, rows: list) -> dict:
    """Fans sap_po_client.fetch_recent_window's single global batch out
    per vendor (called from server.py's background refresh loop) - every
    vendor_code seen in this batch gets its cache updated, AND every
    vendor_code already in the cache gets re-checked even with an empty
    list, so a vendor whose open POs all disappeared this cycle (now
    Finished, or aged out of the tracked window) ends up with an empty
    cache instead of a stale one."""
    grouped = {}
    for r in rows:
        grouped.setdefault(r["vendor_code"], []).append(r)
    already_cached_vendors = db[PO_CACHE_COLLECTION].distinct("vendor_code")
    for vendor_code in set(grouped) | set(already_cached_vendors):
        refresh_po_cache(db, vendor_code, grouped.get(vendor_code, []))
    return {"vendors_updated": len(grouped), "total_line_items": len(rows)}


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


def list_active_po_numbers(db) -> list:
    """Distinct PO numbers currently in the vendor PO cache (any vendor,
    not expired) - the background refresh loop's own worklist."""
    return [r["_id"] for r in db[PO_CACHE_COLLECTION].aggregate([
        {"$match": {"expired": {"$ne": True}}},
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
        shipped = _shipped_qty_so_far(db, vendor_code, it["po_number"], it["item_number"])
        it["already_shipped_qty"] = shipped
        sap_cached = get_sap_open_qty(db, it["po_number"], it["item_number"])
        if sap_cached:
            it["remaining_qty"] = sap_cached["open_qty"]
            it["sap_verified_at"] = sap_cached["fetched_at"]
        else:
            it["remaining_qty"] = round((it.get("po_qty") or 0) - shipped, 4)
            it["sap_verified_at"] = None
        it["buyer_entity_name"] = sap_po_client.buyer_entity_name(it.get("buyer_code"))
    return items


def _generate_doc_code(db) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(DOC_CODE_ALPHABET) for _ in range(DOC_CODE_LENGTH))
        if not db[SHIPMENTS_COLLECTION].find_one({"_id": code}):
            return code
    raise ShipmentError("Could not generate a unique shipment code - please retry")


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
        if ship_qty <= 0:
            raise ShipmentValidationError(f"Ship quantity for item {item_number} on PO {po_number} must be greater than 0")
        already_shipped = _shipped_qty_so_far(db, vendor_code, po_number, item_number, exclude_doc_code=exclude_doc_code)
        # Sep 2 2026, user's explicit ask: prefer SAP's own verified
        # Open PO Quantity (accounts for ANY receipt, including ones
        # posted outside this app) over the locally-computed figure -
        # see get_sap_open_qty()'s docstring. Falls back to the old
        # local computation only if SAP hasn't been read for this item
        # yet.
        sap_cached = get_sap_open_qty(db, po_number, item_number)
        remaining = sap_cached["open_qty"] if sap_cached else (cached.get("po_qty") or 0) - already_shipped
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


def create_shipment(db, account: dict, requested_items: list, vendor_code: str = None) -> dict:
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


def list_shipments(db, status: str = None) -> list:
    query = {"status": status} if status else {}
    return list(db[SHIPMENTS_COLLECTION].find(query).sort("created_at", -1))


def get_shipment_by_code(db, doc_code: str) -> dict:
    doc = db[SHIPMENTS_COLLECTION].find_one({"_id": (doc_code or "").strip().upper()})
    if not doc:
        raise ShipmentNotFoundError("No shipment found for this code")
    buyer_code = shipment_buyer_code(doc)
    doc["buyer_code"] = buyer_code
    doc["buyer_entity_name"] = sap_po_client.buyer_entity_name(buyer_code) if buyer_code else None
    return doc


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


def _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id: str, site_id: str, warehouse_id: str) -> dict:
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
    for it in doc["items"]:
        qty = it.get("actual_qty", it["ship_qty"])
        source_area = f"{site_id}-RM"
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
    bill_date, item_qtys, item_products}} form sap_playwright_
    supplier_pgr_service.post_goods_receipt_via_ui expects - one entry
    per distinct PO, grouping every line item of that PO (user's
    explicit ask). `item_products` lets the Playwright side match each
    grid row by Product ID rather than trusting row order (see that
    module's docstring)."""
    grouped = {}
    for it in doc["items"]:
        po = grouped.setdefault(it["po_number"], {
            "supplier_doc_num": doc.get("supplier_doc_num"),
            "bill_date": doc.get("bill_date"),
            "item_qtys": {},
            "item_products": {},
        })
        po["item_qtys"][it["item_number"]] = it.get("actual_qty", it["ship_qty"])
        po["item_products"][it["item_number"]] = it["product_id"]
    return grouped


def prepare_approval(db, doc_code: str, approved_by: str, supplier_doc_num: str, bill_date: str, site_id: str,
                      warehouse_id: str, item_actual_qtys: dict) -> dict:
    """Records the internal approval unconditionally (staff have already
    physically matched goods + invoice) - sync/fast, the live SAP write
    itself (Playwright, slow) happens as a background job kicked off by
    the caller right after this returns (see server.py's approve
    endpoint). `item_actual_qtys`: {(po_number, item_number): qty} - the
    staff-CONFIRMED received quantity, defaults to the vendor's own
    claimed ship_qty for any line not explicitly overridden."""
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
            "supplier_doc_num": (supplier_doc_num or "").strip() or None, "bill_date": (bill_date or "").strip() or None,
            "site_id": site_id, "warehouse_id": warehouse_id, "sap_sync_status": "pending", "sap_gr_result": None,
            "sap_movement_status": "not_applicable", "sap_movement_result": None,
        }},
    )
    return get_shipment_by_code(db, doc_code)


def finalize_goods_receipt(db, doc_code: str, gr_results: list, goods_movement_client, inventory_client, owner_party_id: str) -> dict:
    """Called after sap_playwright_supplier_pgr_service.
    post_goods_receipt_via_ui returns - `gr_results` is its
    results list. All POs in the shipment must have posted for step 2
    (Goods Movement) to run, matching approve_shipment's old
    all-or-nothing behaviour."""
    doc = get_shipment_by_code(db, doc_code)
    all_ok = bool(gr_results) and all(r.get("status") == "posted" for r in gr_results)
    all_skipped = bool(gr_results) and all(r.get("status") == "skipped" for r in gr_results)
    sap_gr_result = {"ok": all_ok, "per_po": gr_results}
    sap_sync_status = "posted" if all_ok else ("skipped" if all_skipped else "pending")

    sap_movement_status = "not_applicable"
    sap_movement_result = None
    if sap_sync_status == "posted":
        sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id, doc["site_id"], doc["warehouse_id"])
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


def prepare_retry_goods_receipt(db, doc_code: str) -> dict:
    """Validates a shipment is eligible for a fresh Goods Receipt attempt
    (mirrors the old retry_goods_receipt's own guard) - no SAP call here,
    the caller runs the same Playwright job + finalize_goods_receipt used
    by the initial approval."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "approved":
        raise ShipmentValidationError("This shipment has not been approved yet")
    if doc.get("sap_sync_status") == "posted":
        raise ShipmentValidationError("The Goods Receipt has already posted to SAP - nothing to retry")
    return doc



def retry_goods_movement(db, doc_code: str, goods_movement_client, inventory_client, owner_party_id: str) -> dict:
    """Retries ONLY step 2 (the Goods Movement into the chosen warehouse)
    for a shipment whose Goods Receipt (step 1) already posted but the
    movement itself failed/is still pending - mirrors the retry-wip-
    clearing pattern already used elsewhere in this app."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "approved":
        raise ShipmentValidationError("This shipment has not been approved yet")
    if doc.get("sap_sync_status") != "posted":
        raise ShipmentValidationError("The Goods Receipt (step 1) has not posted to SAP yet - nothing to retry")
    if not doc.get("site_id") or not doc.get("warehouse_id"):
        raise ShipmentValidationError("This shipment has no warehouse recorded to retry into")
    sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, inventory_client, owner_party_id, doc["site_id"], doc["warehouse_id"])
    sap_movement_status = "posted" if sap_movement_result.get("ok") else "pending"
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {"sap_movement_status": sap_movement_status, "sap_movement_result": sap_movement_result}},
    )
    return get_shipment_by_code(db, doc_code)
