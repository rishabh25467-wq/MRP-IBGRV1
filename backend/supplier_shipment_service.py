"""Supplier Portal Phase 3 (shipment 2-way match) + Phase 4 (internal
GRN approval -> automated SAP Goods Receipt) - Aug 2026.

Phase 3: an approved vendor picks a Purchase Order + line items from
their cached open-PO list and submits a shipment - validated so the
shipped quantity never exceeds what's still open on that PO item across
ALL of that item's non-rejected shipments (the "2-way match": PO qty vs
shipment qty). Generates an exclusive 6-char alphanumeric, case-
insensitive doc code (stored uppercase) - the physical paperwork/box
label reference used at the dock.

Phase 4: internal staff enter that code, physically match goods +
supplier invoice, and Approve - which immediately attempts to post the
real Goods Receipt to SAP via sap_gsa_write_client (see that module's
docstring for the 2 SAP-side blockers still outstanding). The internal
approval itself is NEVER blocked by SAP being unreachable/not yet
configured - `sap_sync_status` tracks that separately so staff always
know whether SAP has actually received the posting yet.
"""
import secrets
import string
from datetime import datetime, timezone

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
    db[SHIPMENTS_COLLECTION].create_index("po_number")
    db[SHIPMENTS_COLLECTION].create_index("vendor_code")
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


def _shipped_qty_so_far(db, vendor_code: str, po_number: str, item_number: str) -> float:
    pipeline = [
        {"$match": {"vendor_code": vendor_code, "po_number": po_number, "status": {"$in": ["in_transit", "approved"]}}},
        {"$unwind": "$items"},
        {"$match": {"items.item_number": item_number}},
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
    return items


def _generate_doc_code(db) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(DOC_CODE_ALPHABET) for _ in range(DOC_CODE_LENGTH))
        if not db[SHIPMENTS_COLLECTION].find_one({"_id": code}):
            return code
    raise ShipmentError("Could not generate a unique shipment code - please retry")


def create_shipment(db, account: dict, po_number: str, requested_items: list) -> dict:
    if not requested_items:
        raise ShipmentValidationError("Select at least one item to ship")
    cached_by_item = {
        d["item_number"]: d for d in db[PO_CACHE_COLLECTION].find(
            {"vendor_code": account["vendor_code"], "po_number": po_number}
        )
    }
    resolved_items = []
    for req in requested_items:
        item_number = req.get("item_number")
        try:
            ship_qty = float(req.get("ship_qty") or 0)
        except (TypeError, ValueError):
            raise ShipmentValidationError(f"Invalid ship quantity for item {item_number}")
        cached = cached_by_item.get(item_number)
        if not cached:
            raise ShipmentValidationError(f"Item {item_number} was not found on Purchase Order {po_number}")
        if ship_qty <= 0:
            raise ShipmentValidationError(f"Ship quantity for item {item_number} must be greater than 0")
        already_shipped = _shipped_qty_so_far(db, account["vendor_code"], po_number, item_number)
        remaining = (cached.get("po_qty") or 0) - already_shipped
        if ship_qty > remaining + 1e-6:
            raise ShipmentValidationError(f"Item {item_number}: cannot ship {ship_qty} - only {remaining:g} still open on this PO")
        resolved_items.append({
            "item_number": item_number,
            "product_id": cached.get("product_id"),
            "description": cached.get("description"),
            "po_qty": cached.get("po_qty"),
            "already_shipped_qty": already_shipped,
            "ship_qty": ship_qty,
            "unit_of_measure": cached.get("unit_of_measure"),
        })

    doc_code = _generate_doc_code(db)
    now = datetime.now(timezone.utc)
    doc = {
        "_id": doc_code,
        "account_id": account["_id"],
        "vendor_code": account["vendor_code"],
        "company_name": account["company_name"],
        "po_number": po_number,
        "items": resolved_items,
        "status": "in_transit",
        "sap_sync_status": "not_applicable",
        "sap_gr_result": None,
        "created_at": now,
        "approved_at": None,
        "approved_by": None,
        "rejected_at": None,
        "rejected_by": None,
        "rejection_reason": None,
    }
    db[SHIPMENTS_COLLECTION].insert_one(doc)
    return doc


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
    staff from doing their job in this app."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "in_transit":
        raise ShipmentValidationError(f"Shipment is already {doc['status']}")

    sap_sync_status = "pending"
    sap_gr_result = None
    try:
        sap_gr_result = gsa_write_client.post_goods_receipt(
            doc["po_number"], doc["_id"],
            [{"item_id": it["item_number"], "quantity": it["ship_qty"], "unit_of_measure": it.get("unit_of_measure")} for it in doc["items"]],
        )
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
