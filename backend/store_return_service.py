"""Production -> Store "Return to Store" workflow (Sep 2026).

Mirrors store_approval_service.py's issue flow, reversed: that flow moves
stock RM -> SFG (warehouse -> production) when Store issues against a
Stock Request; this one moves it back SFG -> RM (production -> warehouse)
when Store confirms a Return. Reuses the SAME SAP Goods Movement client,
retry/error-clarification helpers, dry-run flag and RM/SFG warehouse ID
resolution as store_approval_service - no new SAP integration.

Two return types:
- "against_request": returning some/all of what Store actually issued
  against a specific `store_requests` doc. Owner party for the SAP call
  is the SAME owner recorded on that original issue (`issued_from_owner`)
  - traceability requirement: Original Request -> Original Issued Qty ->
  Returned Qty -> SAP Goods Movement must always be visible together.
- "manual": no original request - owner party is looked up live from
  inventory_cache (the SFG warehouse's current owner for this product at
  this site), since there's no prior issue record to inherit it from.

Lifecycle: pending ("Awaiting Store") -> under_verification (Store opened
"Process") -> resolved (SAP Goods Movement posted successfully) OR
rejected (Store rejected, reason mandatory - Production can edit and
resubmit the SAME return, which resets it straight back to pending)."""
import logging
from datetime import datetime, timezone

from production_confirmation_service import apply_goods_movement_to_cache, load_stock_by_product, site_locations_for_product
from store_approval_service import _rm_warehouse, _sfg_warehouse, _trigger_goods_movement, is_dry_run

logger = logging.getLogger(__name__)

COLLECTION = "store_returns"
COUNTER_COLLECTION = "store_return_counters"
COUNTER_KEY = "RTN"
_SEQUENCE_WIDTH = 6

REASON_CODES = {
    "unused_material": "Unused Material",
    "excess_material": "Excess Material",
    "production_completed": "Production Completed",
    "wrong_material_issued": "Wrong Material Issued",
    "production_cancelled": "Production Cancelled",
    "material_not_required": "Material Not Required",
    "damaged_material": "Damaged Material",
    "other": "Other",
}

STATUS_LABELS = {
    "pending": "Awaiting Store",
    "under_verification": "Under Verification",
    "resolved": "Resolved",
    "rejected": "Rejected",
}


def ensure_indexes(db) -> None:
    db[COLLECTION].create_index("status")
    db[COLLECTION].create_index("original_request_id")
    db[COLLECTION].create_index("requester")


def _generate_return_id(db) -> str:
    doc = db[COUNTER_COLLECTION].find_one_and_update(
        {"_id": COUNTER_KEY}, {"$inc": {"seq": 1}}, upsert=True, return_document=True,
    )
    return f"RTN-{doc['seq']:0{_SEQUENCE_WIDTH}d}"


def _validate_reason(reason_code: str, remarks: str) -> None:
    if reason_code not in REASON_CODES:
        raise ValueError("Invalid return reason")
    if reason_code == "other" and not (remarks or "").strip():
        raise ValueError("Remarks are required when reason is 'Other'")


def already_returned_qty(db, original_request_id: str, product_id: str) -> float:
    """Sum of return_qty for this request+item across every return that
    actually reached SAP (status == "resolved") - a pending/rejected
    return never actually removed stock, so it must not count against
    how much is still available to return."""
    total = 0.0
    for doc in db[COLLECTION].find({"original_request_id": original_request_id, "status": "resolved"}, {"items": 1}):
        for it in doc.get("items", []):
            if it["product_id"] == product_id:
                total += it.get("return_qty") or 0
    return round(total, 4)


def list_previous_requests(db, requester: str, is_admin: bool, site_filter: str = None) -> list:
    """Step 1 of Return Against Request - only requests with at least one
    actually-issued component are relevant to return."""
    match = {"components.issued_qty": {"$gt": 0}}
    if not is_admin:
        match["requester"] = requester
    if site_filter and site_filter != "all":
        match["site_id"] = site_filter
    return list(db["store_requests"].find(match, {"components": 0}).sort("created_at", -1).limit(200))


def get_issued_items_for_request(db, request_id: str) -> dict | None:
    """Step 2 - items actually issued by Store against this request, with
    Already Returned/Available-To-Return computed live."""
    req = db["store_requests"].find_one({"_id": request_id})
    if not req:
        return None
    items = []
    for c in req.get("components", []):
        issued_qty = c.get("issued_qty") or 0
        if issued_qty <= 0:
            continue
        returned = already_returned_qty(db, request_id, c["product_id"])
        items.append({
            "product_id": c["product_id"], "description": c.get("description"),
            "unit_of_measure": c.get("unit_of_measure"), "required_qty": c.get("required_qty"), "issued_qty": issued_qty,
            "already_returned_qty": returned, "available_to_return": round(max(0.0, issued_qty - returned), 4),
            "issued_from_owner": c.get("issued_from_owner"), "issued_from_warehouse": c.get("issued_from_warehouse"),
            "issued_via_sap": bool((c.get("goods_movement") or {}).get("ok")),
        })
    return {"request_id": request_id, "site_id": req["site_id"], "requester": req.get("requester"), "items": items}


def _build_items(db, return_type: str, original_request_id: str, site_id: str, items: list) -> tuple[list, str]:
    if not items:
        raise ValueError("At least one item is required")
    clean_items = []
    if return_type == "against_request":
        if not original_request_id:
            raise ValueError("original_request_id is required for Return Against Request")
        info = get_issued_items_for_request(db, original_request_id)
        if not info:
            raise ValueError("Original stock request not found")
        by_product = {it["product_id"]: it for it in info["items"]}
        for it in items:
            src = by_product.get(it["product_id"])
            if not src:
                raise ValueError(f"{it['product_id']} was not issued against this request")
            return_qty = float(it["return_qty"])
            if return_qty <= 0:
                raise ValueError("Return quantity must be greater than zero")
            if return_qty > src["available_to_return"] + 1e-6:
                raise ValueError(f"Return quantity for {it['product_id']} exceeds what's still available to return ({src['available_to_return']})")
            _validate_reason(it["reason_code"], it.get("remarks"))
            clean_items.append({
                "product_id": it["product_id"], "description": src.get("description"),
                "unit_of_measure": src.get("unit_of_measure"),
                "issued_qty": src["issued_qty"], "already_returned_qty": src["already_returned_qty"],
                "return_qty": return_qty, "reason_code": it["reason_code"],
                "reason_label": REASON_CODES[it["reason_code"]], "remarks": (it.get("remarks") or "").strip() or None,
                "issued_from_owner": src.get("issued_from_owner"), "issued_from_warehouse": src.get("issued_from_warehouse"),
                "issued_via_sap": src.get("issued_via_sap", True), "sap_goods_movement": None,
            })
        return clean_items, info["site_id"]

    if return_type == "manual":
        if not site_id:
            raise ValueError("site_id is required for Manual Return")
        for it in items:
            return_qty = float(it["qty"])
            if return_qty <= 0:
                raise ValueError("Quantity must be greater than zero")
            if not (it.get("product_id") or "").strip():
                raise ValueError("Item Code is required")
            _validate_reason(it["reason_code"], it.get("remarks"))
            clean_items.append({
                "product_id": it["product_id"].strip(), "description": (it.get("description") or "").strip() or None,
                "unit_of_measure": it.get("unit_of_measure") or "EA",
                "issued_qty": None, "already_returned_qty": None, "return_qty": return_qty,
                "reason_code": it["reason_code"], "reason_label": REASON_CODES[it["reason_code"]],
                "remarks": (it.get("remarks") or "").strip() or None,
                "issued_from_owner": None, "issued_from_warehouse": None, "sap_goods_movement": None,
            })
        return clean_items, site_id

    raise ValueError("Invalid return type")


def create_return(db, return_type: str, requester: str, site_id: str, original_request_id: str, items: list) -> dict:
    clean_items, resolved_site_id = _build_items(db, return_type, original_request_id, site_id, items)
    now = datetime.now(timezone.utc)
    doc = {
        "_id": _generate_return_id(db), "return_type": return_type,
        "original_request_id": original_request_id if return_type == "against_request" else None,
        "site_id": resolved_site_id, "requester": requester, "items": clean_items,
        "status": "pending", "rejection_reason": None, "store_actor": None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    }
    db[COLLECTION].insert_one(doc)
    return doc


def update_rejected_return(db, return_id: str, requester: str, items: list) -> dict | None:
    """Production editing + resubmitting a REJECTED return (user's
    explicit choice - same RTN id, not a brand-new request). Resets
    straight back to "pending" ("Awaiting Store")."""
    doc = db[COLLECTION].find_one({"_id": return_id})
    if not doc:
        return None
    if doc["status"] != "rejected":
        raise ValueError(f"Only a rejected return request can be edited (current status: {doc['status']})")
    if (doc.get("requester") or "").strip().lower() != (requester or "").strip().lower():
        raise ValueError("You can only edit your own return request")
    clean_items, resolved_site_id = _build_items(db, doc["return_type"], doc.get("original_request_id"), doc["site_id"], items)
    now = datetime.now(timezone.utc)
    db[COLLECTION].update_one({"_id": return_id}, {"$set": {
        "items": clean_items, "site_id": resolved_site_id, "status": "pending",
        "rejection_reason": None, "store_actor": None, "updated_at": now,
    }})
    return db[COLLECTION].find_one({"_id": return_id})


def list_returns(db, requester: str = None, is_admin: bool = False, site_filter: str = None) -> list:
    match = {}
    if not is_admin and requester:
        match["requester"] = requester
    if site_filter and site_filter != "all":
        match["site_id"] = site_filter
    return list(db[COLLECTION].find(match).sort("created_at", -1).limit(500))


def get_return(db, return_id: str) -> dict | None:
    return db[COLLECTION].find_one({"_id": return_id})


def list_pending_for_store(db, site_filter: str = None) -> list:
    match = {"status": {"$in": ["pending", "under_verification"]}}
    if site_filter and site_filter != "all":
        match["site_id"] = site_filter
    return list(db[COLLECTION].find(match).sort("created_at", -1))


def start_process(db, return_id: str, store_actor: str) -> dict | None:
    doc = db[COLLECTION].find_one({"_id": return_id})
    if not doc:
        return None
    if doc["status"] not in ("pending", "under_verification"):
        raise ValueError(f"This return is not awaiting store action (current status: {doc['status']})")
    db[COLLECTION].update_one({"_id": return_id}, {"$set": {
        "status": "under_verification", "store_actor": store_actor, "updated_at": datetime.now(timezone.utc),
    }})
    return db[COLLECTION].find_one({"_id": return_id})


def reject_return(db, return_id: str, store_actor: str, reason: str) -> dict | None:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Rejection reason is required")
    doc = db[COLLECTION].find_one({"_id": return_id})
    if not doc:
        return None
    if doc["status"] not in ("pending", "under_verification"):
        raise ValueError(f"This return is not awaiting store action (current status: {doc['status']})")
    db[COLLECTION].update_one({"_id": return_id}, {"$set": {
        "status": "rejected", "rejection_reason": reason, "store_actor": store_actor, "updated_at": datetime.now(timezone.utc),
    }})
    return db[COLLECTION].find_one({"_id": return_id})


def start_confirm(db, return_id: str, store_actor: str) -> dict | None:
    """Fast, synchronous half - flips status to "under_verification" (if
    not already) so the slow SAP calls can run as a background job, same
    split as store_approval_service.start_issue/run_issue_movements."""
    doc = db[COLLECTION].find_one({"_id": return_id})
    if not doc:
        return None
    if doc["status"] not in ("pending", "under_verification"):
        raise ValueError(f"This return is not awaiting store action (current status: {doc['status']})")
    db[COLLECTION].update_one({"_id": return_id}, {"$set": {
        "status": "under_verification", "store_actor": store_actor, "updated_at": datetime.now(timezone.utc),
    }})
    return db[COLLECTION].find_one({"_id": return_id})


def _resolve_manual_item_owner(db, site_id: str, product_id: str) -> str | None:
    """No prior issue record for a Manual Return - look up who currently
    owns this product's stock at the site's SFG (production) warehouse,
    the same way store_approval_service finds an RM-side owner."""
    stock_by_product = load_stock_by_product(db)
    locations = site_locations_for_product(stock_by_product, product_id, site_id) or []
    sfg_id = _sfg_warehouse(site_id)
    loc = next((l for l in locations if l.get("warehouse_id") == sfg_id and l.get("owner")), None)
    return loc.get("owner") if loc else None


def run_confirm_movements(db, return_id: str, sap_client, progress_cb=None) -> dict:
    """The slow part - one SAP Goods Movement per item, SFG -> RM (the
    exact reverse of store_approval_service's issue movement). Persists
    each item's result right after its own call, same as
    run_issue_movements, so progress is visible mid-run. A retry (Store
    clicking Confirm again after a partial SAP failure) skips items that
    already succeeded and only retries the ones that didn't."""
    doc = db[COLLECTION].find_one({"_id": return_id})
    if not doc:
        raise ValueError("Return request not found")
    site_id = doc["site_id"]
    source_warehouse = _sfg_warehouse(site_id)  # material currently sits with production
    target_warehouse = _rm_warehouse(site_id)   # returning it to the Raw Material store
    items = doc["items"]
    total = len(items)

    for idx, it in enumerate(items):
        if (it.get("sap_goods_movement") or {}).get("ok"):
            if progress_cb:
                progress_cb(idx + 1, total, it["product_id"])
            continue
        owner = it.get("issued_from_owner") or _resolve_manual_item_owner(db, site_id, it["product_id"])
        if owner:
            movement = _trigger_goods_movement(
                sap_client, owner, it["product_id"], source_warehouse, target_warehouse,
                it["return_qty"], it.get("unit_of_measure") or "EA", site_id,
            )
            if movement.get("ok") and not is_dry_run():
                apply_goods_movement_to_cache(db, it["product_id"], source_warehouse, target_warehouse, it["return_qty"])
        elif it.get("issued_via_sap") is False:
            # Sep 9 2026, real case found: the original Stock Request marked
            # this line "Issued" in our own bookkeeping, but its own
            # goods_movement was never actually posted to SAP at the time
            # (e.g. "No stock on file in this site's RM warehouse") - there
            # is genuinely nothing in SAP to reverse for this item.
            movement = {"attempted": False, "ok": False, "reason": "This item's original issue was never actually posted to SAP (no real SAP movement exists for it) - there is nothing to return in SAP. Ask your Store admin to review the original Stock Request."}
        else:
            movement = {"attempted": False, "ok": False, "reason": "Could not determine the owning party for this material at the source location - contact IT."}
        it["sap_goods_movement"] = movement
        db[COLLECTION].update_one({"_id": return_id}, {"$set": {f"items.{idx}": it}})
        if progress_cb:
            progress_cb(idx + 1, total, it["product_id"])

    now = datetime.now(timezone.utc)
    any_failed = any(not (it.get("sap_goods_movement") or {}).get("ok") for it in items)
    if any_failed:
        update = {"updated_at": now}  # stays "under_verification" - Store can retry
    else:
        update = {"status": "resolved", "resolved_at": now, "updated_at": now}
    db[COLLECTION].update_one({"_id": return_id}, {"$set": update})
    return db[COLLECTION].find_one({"_id": return_id})
