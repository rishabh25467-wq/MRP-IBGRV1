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
supplier invoice, and Approve - which immediately attempts to post the
real Goods Receipt to SAP via sap_gsa_write_client, ONE call PER
distinct PO number in the shipment (a shipment can now span multiple
POs - see above). The internal approval itself is NEVER blocked by SAP
being unreachable/not yet configured - `sap_sync_status` tracks that
separately so staff always know whether SAP has actually received the
posting yet.
"""
import secrets
import string
from datetime import datetime, timezone

import sap_po_client

PO_CACHE_COLLECTION = "supplier_portal_po_cache"
SHIPMENTS_COLLECTION = "supplier_portal_shipments"
DOC_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")
DOC_CODE_LENGTH = 6


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

    Also DELETES any of this vendor's previously-cached rows that are no
    longer in the fresh live fetch (PO now Finished, or fell out of
    sap_po_client's current-window fetch) - a live fetch is the source
    of truth, so a stale/no-longer-open row must not linger forever
    (found by user report Aug 28 2026: the cache kept showing PO rows
    from before the sap_po_client recency-window fix even after that
    fix landed, since nothing had ever deleted them).

    Only rows tagged `source="sap_live"` are eligible for that delete -
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
            {"_id": key}, {"$set": {**it, "vendor_code": vendor_code, "source": "sap_live", "updated_at": now}}, upsert=True,
        )
    db[PO_CACHE_COLLECTION].delete_many({"vendor_code": vendor_code, "source": "sap_live", "_id": {"$nin": fresh_keys}})


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


def get_cached_pos_with_remaining(db, vendor_code: str) -> list:
    items = list(db[PO_CACHE_COLLECTION].find({"vendor_code": vendor_code}, {"_id": 0}).sort("po_number", 1))
    for it in items:
        shipped = _shipped_qty_so_far(db, vendor_code, it["po_number"], it["item_number"])
        it["already_shipped_qty"] = shipped
        it["remaining_qty"] = round((it.get("po_qty") or 0) - shipped, 4)
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
        cached = db[PO_CACHE_COLLECTION].find_one({"vendor_code": vendor_code, "po_number": po_number, "item_number": item_number})
        if not cached:
            raise ShipmentValidationError(f"Item {item_number} was not found on Purchase Order {po_number}")
        if ship_qty <= 0:
            raise ShipmentValidationError(f"Ship quantity for item {item_number} on PO {po_number} must be greater than 0")
        already_shipped = _shipped_qty_so_far(db, vendor_code, po_number, item_number, exclude_doc_code=exclude_doc_code)
        remaining = (cached.get("po_qty") or 0) - already_shipped
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
        })
    return resolved


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
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "approved_by": None,
        "rejected_at": None,
        "rejected_by": None,
        "rejection_reason": None,
    }
    db[SHIPMENTS_COLLECTION].insert_one(doc)
    return doc


def update_shipment_items(db, account: dict, doc_code: str, requested_items: list) -> dict:
    """Lets a vendor add/remove/change quantities on their OWN shipment
    for as long as it's still `in_transit` - locked the moment it's
    Approved or Rejected (user's explicit ask, Aug 28 2026). Scoped by
    the SHIPMENT's own vendor_code (set at creation, possibly under a
    testing-mode impersonation - see create_shipment above), not the
    account's own vendor_code, so editing stays consistent regardless
    of which vendor was being viewed when it was created."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["account_id"] != account["_id"]:
        raise ShipmentNotFoundError("No shipment found for this code")
    if doc["status"] != "in_transit":
        raise ShipmentValidationError("This shipment has already been processed and can no longer be changed")
    resolved_items = _resolve_items(db, doc["vendor_code"], requested_items, exclude_doc_code=doc_code)
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]}, {"$set": {"items": resolved_items, "updated_at": datetime.now(timezone.utc)}},
    )
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
    return doc


def reject_shipment(db, doc_code: str, rejected_by: str, reason: str) -> dict:
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "in_transit":
        raise ShipmentValidationError(f"Shipment is already {doc['status']}")
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "status": "rejected", "rejected_at": datetime.now(timezone.utc),
            "rejected_by": rejected_by, "rejection_reason": (reason or "").strip() or None,
        }},
    )
    return get_shipment_by_code(db, doc_code)


def approve_shipment(db, doc_code: str, approved_by: str, gsa_write_client) -> dict:
    """Internal approval is recorded unconditionally (staff have already
    physically matched goods + invoice) - the SAP posting attempt is
    best-effort and tracked separately via sap_sync_status, so a SAP
    outage or the still-outstanding write-access blocker never stops
    staff from doing their job in this app. Posts ONE Goods Receipt call
    PER distinct PO number in the shipment (a shipment can now span
    multiple POs)."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "in_transit":
        raise ShipmentValidationError(f"Shipment is already {doc['status']}")

    grouped_by_po = {}
    for it in doc["items"]:
        grouped_by_po.setdefault(it["po_number"], []).append(it)

    sap_sync_status = "pending"
    sap_gr_result = None
    try:
        per_po_results = []
        for po_number, items in grouped_by_po.items():
            result = gsa_write_client.post_goods_receipt(
                po_number, doc["_id"],
                [{"item_id": it["item_number"], "quantity": it["ship_qty"], "unit_of_measure": it.get("unit_of_measure")} for it in items],
            )
            per_po_results.append({"po_number": po_number, **result})
        sap_gr_result = {"ok": True, "per_po": per_po_results}
        sap_sync_status = "posted"
    except Exception as e:
        # Deliberately broad, not just SAPGSAWriteError - the internal
        # approval (staff has already physically matched goods+invoice)
        # must NEVER 500/block on ANY surprise from the SAP posting
        # attempt, expected or not (found by testing_agent, iteration_121).
        sap_gr_result = {"ok": False, "reason": str(e)}

    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "status": "approved", "approved_at": datetime.now(timezone.utc), "approved_by": approved_by,
            "sap_sync_status": sap_sync_status, "sap_gr_result": sap_gr_result,
        }},
    )
    return get_shipment_by_code(db, doc_code)
