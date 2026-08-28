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
supplier invoice, and Approve - which immediately attempts a 2-STEP live
SAP write: (1) posts the real Goods Receipt via sap_gsa_write_client, ONE
call PER distinct PO number in the shipment (a shipment can now span
multiple POs), then (2) - since the GSA schema itself has NO field for
warehouse/quality-status (confirmed against SAP's own help docs, Aug 28
2026) - a follow-up Goods Movement (sap_goods_movement_client, reused
from Store Approval) moves each line's received qty into the receiver's
chosen warehouse. NOTE (confirmed live, Aug 28 2026): forcing an
explicit RESTRICTED/Quality-Inspection stock status on that movement is
NOT achievable on this tenant as currently configured - see
_post_goods_movement_for_items's docstring below for the live test that
proved this; step 2 today only does a PLAIN move into the chosen
warehouse. The internal approval itself is NEVER blocked by SAP being
unreachable - `sap_sync_status`/`sap_movement_status` track each step
separately so staff always know exactly how far a shipment got.

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


def _post_goods_movement_for_items(db, doc, goods_movement_client, owner_party_id: str, site_id: str, warehouse_id: str) -> dict:
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
    silently claimed as working."""
    per_item = []
    all_ok = True
    for it in doc["items"]:
        try:
            result = goods_movement_client.goods_movement(
                owner_party_id=owner_party_id, product_id=it["product_id"],
                source_logistics_area_id=f"{site_id}-RM", target_logistics_area_id=warehouse_id,
                quantity=it["ship_qty"], quantity_uom=it.get("unit_of_measure") or "EA", site_id=site_id,
                dry_run=False,
            )
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        if not result.get("ok"):
            all_ok = False
        per_item.append({"po_number": it["po_number"], "item_number": it["item_number"], "product_id": it["product_id"], **result})
    return {"ok": all_ok, "per_item": per_item}


def approve_shipment(db, doc_code: str, approved_by: str, supplier_doc_num: str, site_id: str, warehouse_id: str,
                      gsa_write_client, goods_movement_client, owner_party_id: str) -> dict:
    """Internal approval is recorded unconditionally (staff have already
    physically matched goods + invoice) - the SAP posting attempt is
    best-effort and tracked separately via sap_sync_status/
    sap_movement_status, so a SAP outage never stops staff from doing
    their job in this app. Two-step live write: (1) ONE Goods Receipt
    call PER distinct PO number in the shipment, then (2), only if step
    1 fully succeeded, a Goods Movement per line item into the chosen
    warehouse (PLAIN move only - see _post_goods_movement_for_items'
    docstring for the confirmed RESTRICTED/Quality-Inspection limitation)."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] not in ("in_transit", "discrepancy"):
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

    sap_movement_status = "not_applicable"
    sap_movement_result = None
    if sap_sync_status == "posted":
        sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, owner_party_id, site_id, warehouse_id)
        sap_movement_status = "posted" if sap_movement_result.get("ok") else "pending"
    else:
        sap_movement_result = {"ok": False, "reason": "Skipped - Goods Receipt (step 1) did not succeed yet"}

    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "status": "approved", "approved_at": datetime.now(timezone.utc), "approved_by": approved_by,
            "supplier_doc_num": (supplier_doc_num or "").strip() or None, "site_id": site_id, "warehouse_id": warehouse_id,
            "sap_sync_status": sap_sync_status, "sap_gr_result": sap_gr_result,
            "sap_movement_status": sap_movement_status, "sap_movement_result": sap_movement_result,
        }},
    )
    return get_shipment_by_code(db, doc_code)


def retry_goods_movement(db, doc_code: str, goods_movement_client, owner_party_id: str) -> dict:
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
    sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, owner_party_id, doc["site_id"], doc["warehouse_id"])
    sap_movement_status = "posted" if sap_movement_result.get("ok") else "pending"
    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {"sap_movement_status": sap_movement_status, "sap_movement_result": sap_movement_result}},
    )
    return get_shipment_by_code(db, doc_code)


def retry_goods_receipt(db, doc_code: str, gsa_write_client, goods_movement_client, owner_party_id: str) -> dict:
    """Retries step 1 (the Goods Receipt/GSA call) for a shipment that's
    already `approved` locally but whose SAP write failed (Aug 28 2026 -
    transient "Web service processing error" on SAP's own side, distinct
    from the STO Inbound Receipt "action is disabled" issue). Mirrors
    retry_goods_movement above - approve_shipment itself can't be
    re-called since it's gated on status not yet being "approved"."""
    doc = get_shipment_by_code(db, doc_code)
    if doc["status"] != "approved":
        raise ShipmentValidationError("This shipment has not been approved yet")
    if doc.get("sap_sync_status") == "posted":
        raise ShipmentValidationError("The Goods Receipt has already posted to SAP - nothing to retry")

    grouped_by_po = {}
    for it in doc["items"]:
        grouped_by_po.setdefault(it["po_number"], []).append(it)
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
        sap_gr_result = {"ok": False, "reason": str(e)}
        sap_sync_status = "pending"

    sap_movement_status = doc.get("sap_movement_status") or "not_applicable"
    sap_movement_result = doc.get("sap_movement_result")
    if sap_sync_status == "posted" and doc.get("site_id") and doc.get("warehouse_id"):
        sap_movement_result = _post_goods_movement_for_items(db, doc, goods_movement_client, owner_party_id, doc["site_id"], doc["warehouse_id"])
        sap_movement_status = "posted" if sap_movement_result.get("ok") else "pending"

    db[SHIPMENTS_COLLECTION].update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "sap_sync_status": sap_sync_status, "sap_gr_result": sap_gr_result,
            "sap_movement_status": sap_movement_status, "sap_movement_result": sap_movement_result,
        }},
    )
    return get_shipment_by_code(db, doc_code)
