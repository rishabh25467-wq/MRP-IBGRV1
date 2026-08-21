"""Mongo-backed support for the Production Task Confirmation page:
- `deviation_reason_master`: editable dropdown taxonomy for the SOAP
  DeviationReason code (free 4-char token in SAP, tenant-configurable -
  seeded here with SAP ByDesign's own standard/default code list per
  help.sap.com, editable via the page's "Manage Reasons" action in case
  this tenant's Fine-Tuning activity differs).
- `production_confirmation_history`: append-only audit log of every
  confirmation submitted (who/when/what/result) - same pattern as
  po_selection_service.py's audit trail, no login system exists so
  "who" is a free-text actor name from the browser.
"""
from datetime import datetime, timezone

REASON_COLLECTION = "deviation_reason_master"
HISTORY_COLLECTION = "production_confirmation_history"

DEFAULT_DEVIATION_REASONS = [
    {"code": "001", "label": "Resource Failure"},
    {"code": "002", "label": "Resource Unclean"},
    {"code": "003", "label": "Maintenance"},
    {"code": "004", "label": "Tool Missing"},
    {"code": "005", "label": "Tool Broken"},
    {"code": "006", "label": "Material Damage"},
    {"code": "007", "label": "Quality Issue"},
    {"code": "008", "label": "Invalid Operation"},
    {"code": "011", "label": "Missing Part"},
    {"code": "020", "label": "Peak Time"},
    {"code": "021", "label": "Unplanned Order"},
    {"code": "Z10", "label": "Broken Parts"},
]


def get_deviation_reasons(db) -> list:
    collection = db[REASON_COLLECTION]
    if collection.count_documents({}) == 0:
        now = datetime.now(timezone.utc)
        collection.insert_many([{"_id": r["code"], "label": r["label"], "source": "default", "created_at": now} for r in DEFAULT_DEVIATION_REASONS])
    return [{"code": doc["_id"], "label": doc["label"]} for doc in collection.find({}).sort("_id", 1)]


def add_deviation_reason(db, code: str, label: str) -> list:
    code = code.strip()
    label = label.strip()
    if not code or not label:
        return get_deviation_reasons(db)
    get_deviation_reasons(db)  # ensure seeded first
    db[REASON_COLLECTION].update_one(
        {"_id": code},
        {"$set": {"label": label, "source": "manual", "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return get_deviation_reasons(db)


def delete_deviation_reason(db, code: str) -> list:
    db[REASON_COLLECTION].delete_one({"_id": code})
    return get_deviation_reasons(db)


def log_confirmation(db, actor: str, request_payload: dict, result: dict) -> None:
    db[HISTORY_COLLECTION].insert_one({
        "actor": actor,
        "production_lot_id": request_payload.get("production_lot_id"),
        "reporting_point_id": request_payload.get("reporting_point_id"),
        "main_output_product": request_payload.get("main_output_product"),
        "confirmed_quantity": request_payload.get("confirmed_quantity"),
        "confirmed_scrap": request_payload.get("confirmed_scrap"),
        "deviation_reason_code": request_payload.get("deviation_reason_code"),
        "confirmation_finished": request_payload.get("confirmation_finished"),
        "success": result.get("success"),
        "logs": result.get("logs"),
        "wip_clearing": result.get("wip_clearing"),
        "byproduct_material_output_uuid": request_payload.get("byproduct_material_output_uuid"),
        "byproduct_confirmed_quantity": request_payload.get("byproduct_confirmed_quantity"),
        "byproduct_unit_code": request_payload.get("byproduct_unit_code"),
        "byproduct_confirmation": result.get("byproduct_confirmation"),
        "at": datetime.now(timezone.utc),
    })


def get_confirmation_history(db, production_lot_id: str = None, limit: int = 200) -> list:
    query = {}
    if production_lot_id:
        query["production_lot_id"] = production_lot_id
    docs = db[HISTORY_COLLECTION].find(query).sort("at", -1).limit(limit)
    return [
        {
            "actor": d.get("actor"),
            "production_lot_id": d.get("production_lot_id"),
            "reporting_point_id": d.get("reporting_point_id"),
            "main_output_product": d.get("main_output_product"),
            "confirmed_quantity": d.get("confirmed_quantity"),
            "confirmed_scrap": d.get("confirmed_scrap"),
            "deviation_reason_code": d.get("deviation_reason_code"),
            "confirmation_finished": d.get("confirmation_finished"),
            "success": d.get("success"),
            "logs": d.get("logs"),
            "wip_clearing": d.get("wip_clearing"),
            "byproduct_confirmed_quantity": d.get("byproduct_confirmed_quantity"),
            "byproduct_unit_code": d.get("byproduct_unit_code"),
            "byproduct_confirmation": d.get("byproduct_confirmation"),
            "at": d["at"].isoformat(),
        }
        for d in docs
    ]


PROPOSAL_HISTORY_COLLECTION = "production_order_creation_history"


def log_proposal_creation(db, actor: str, request_payload: dict, result: dict, job_id: str = None) -> None:
    """job_id (when this came from the one-click create-and-release job, not
    the standalone create-proposal-only endpoint) is stored so a later
    log_order_release call for the SAME job can update this exact row
    in-place instead of appearing as a disconnected second row - lets the
    history table show "Proposal 223835 -> Order 69959" together."""
    db[PROPOSAL_HISTORY_COLLECTION].insert_one({
        "type": "proposal_created",
        "job_id": job_id,
        "actor": actor,
        "material_id": request_payload.get("material_id"),
        "site_id": request_payload.get("site_id"),
        "quantity": request_payload.get("quantity"),
        "unit_code": request_payload.get("unit_code"),
        "production_proposal_id": result.get("production_proposal_id"),
        "at": datetime.now(timezone.utc),
    })


def log_order_release(db, actor: str, production_order_id: str, result: dict, job_id: str = None) -> None:
    """When job_id matches the same one-click job's proposal_created row,
    update that row in-place with the order outcome instead of inserting a
    disconnected second row. Falls back to a standalone insert (previous
    behavior) for the manual/standalone Release-an-existing-Order form,
    which has no job_id."""
    if job_id:
        updated = db[PROPOSAL_HISTORY_COLLECTION].update_one(
            {"job_id": job_id, "type": "proposal_created"},
            {"$set": {
                "production_order_id": production_order_id,
                "released": result.get("success"),
                "released_at": datetime.now(timezone.utc),
                "released_by": actor,
            }},
        )
        if updated.matched_count:
            return
    db[PROPOSAL_HISTORY_COLLECTION].insert_one({
        "type": "order_released",
        "actor": actor,
        "production_order_id": production_order_id,
        "success": result.get("success"),
        "at": datetime.now(timezone.utc),
    })


def get_proposal_and_release_history(db, limit: int = 200) -> list:
    docs = db[PROPOSAL_HISTORY_COLLECTION].find({}).sort("at", -1).limit(limit)
    return [
        {
            "type": d.get("type"),
            "actor": d.get("actor"),
            "material_id": d.get("material_id"),
            "site_id": d.get("site_id"),
            "quantity": d.get("quantity"),
            "unit_code": d.get("unit_code"),
            "production_proposal_id": d.get("production_proposal_id"),
            "production_order_id": d.get("production_order_id"),
            "released": d.get("released"),
            "released_by": d.get("released_by"),
            "success": d.get("success"),
            "at": d["at"].isoformat(),
        }
        for d in docs
    ]


def _check_availability_against_stock(bom_doc: dict, stock_by_product: dict, confirmed_quantity: float, site_id: str) -> dict:
    if not bom_doc or not bom_doc.get("groups"):
        return {"checked": False, "reason": "No cached BOM found locally for this product - cannot check component availability.", "components": []}
    components = []
    for group in bom_doc["groups"]:
        for item in group["items"]:
            if not item.get("active") or item.get("quantity") is None:
                continue
            required_qty = round(item["quantity"] * confirmed_quantity, 4)
            locations = stock_by_product.get(item["product_id"])
            if locations is None:
                available_qty = None
            else:
                # inventory_cache stores full site names like "RADISH TECHNOLOGY-P2"
                # (company name + site code), never the bare site code - match on
                # the "-{site_id}" suffix, not exact equality (was always 0 before).
                available_qty = sum(loc["qty"] for loc in locations if loc.get("site", "").endswith(f"-{site_id}"))
            components.append({
                "product_id": item["product_id"],
                "description": item.get("description"),
                "unit_of_measure": item.get("unit_of_measure"),
                "required_qty": required_qty,
                "available_qty": available_qty,
                # Needing 0 of a component (e.g. Open Quantity is already 0 -
                # a fully-confirmed row) is never "short", regardless of
                # whether we happen to have on-hand data for it.
                "sufficient": required_qty <= 0 or (available_qty is not None and available_qty >= required_qty),
            })
    return {"checked": True, "reason": None, "components": components}


def check_component_availability(db, main_output_product: str, confirmed_quantity: float, site_id: str, sap_inventory_client=None) -> dict:
    """Compares BOM component requirements (from the app's own bom_node_cache,
    scaled to the quantity about to be confirmed) against on-hand stock at
    the lot's site - lets a user see BEFORE confirming/releasing whether
    SAP's backflush is likely to reject it for insufficient component
    stock. If `sap_inventory_client` is given, fetches LIVE stock from SAP
    (this is the one stock check in the app that does - it gates a real
    SAP write, an out-of-date cache here directly caused a wrong "unknown"
    result once, see PRD Aug 2026) - falls back to the cached snapshot if
    the live call fails (SAP's inventory report has occasional transient
    errors) so a live SAP hiccup never blocks a release outright. Without
    a client, or on live failure, uses the cached `inventory_cache`
    snapshot (refreshed on a fixed schedule - see INVENTORY_CACHE_REFRESH_
    INTERVAL_SECONDS in server.py) - this is what the open-lots list's
    Stock badges use, deliberately kept cache-only/instant since it's a
    glance-view checked on every page load, not a gate before a write."""
    bom_doc = db["bom_node_cache"].find_one({"_id": main_output_product})
    stock_by_product = None
    if sap_inventory_client is not None:
        try:
            live_rows = sap_inventory_client.get_inventory_detail()
            stock_by_product = {}
            for row in live_rows:
                stock_by_product.setdefault(row["product_id"], []).append({"site": row.get("site"), "qty": row["qty"]})
        except Exception:
            stock_by_product = None  # fall through to cache below
    if stock_by_product is None:
        inventory_doc = db["inventory_cache"].find_one({"_id": "latest"})
        stock_by_product = {}
        for item in (inventory_doc or {}).get("items", []):
            stock_by_product[item["product_id"]] = item.get("locations", [])
    return _check_availability_against_stock(bom_doc, stock_by_product, confirmed_quantity, site_id)


def check_component_availability_batch(db, rows: list) -> list:
    """Batch counterpart of check_component_availability() for the open-lots
    list - fetches inventory_cache and every needed bom_node_cache doc ONCE
    (not once per row) so a 100+ row list stays instant. `rows` is a list
    of {main_output_product, quantity, site_id}; returns a same-length list
    of {checked, reason, sufficient_all, short_components} - a compact
    summary (not the full component list) since the list view only needs a
    badge + the short ones for a tooltip, not every sufficient component."""
    inventory_doc = db["inventory_cache"].find_one({"_id": "latest"})
    stock_by_product = {}
    for item in (inventory_doc or {}).get("items", []):
        stock_by_product[item["product_id"]] = item.get("locations", [])

    product_ids = {r["main_output_product"] for r in rows if r.get("main_output_product")}
    bom_docs = {d["_id"]: d for d in db["bom_node_cache"].find({"_id": {"$in": list(product_ids)}})}

    results = []
    for r in rows:
        bom_doc = bom_docs.get(r.get("main_output_product"))
        result = _check_availability_against_stock(bom_doc, stock_by_product, r.get("quantity") or 0, r.get("site_id"))
        short = [c for c in result["components"] if not c["sufficient"]]
        results.append({
            "checked": result["checked"],
            "reason": result["reason"],
            "sufficient_all": result["checked"] and len(short) == 0,
            "short_components": short,
        })
    return results
