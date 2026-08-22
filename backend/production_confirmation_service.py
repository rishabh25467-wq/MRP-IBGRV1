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
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REASON_COLLECTION = "deviation_reason_master"
HISTORY_COLLECTION = "production_confirmation_history"

# Aug 2026: SAP's own stock-status field can carry values like "Inspection"
# (Quality Inspection hold) or "Blocked" - this stock physically sits in
# the warehouse but is NOT free/usable stock. Confirmed live in this
# tenant's real inventory_cache data ("Inspection" appears alongside the
# normal "Not Assigned" = unrestricted status). Never count this stock as
# "available" for a component availability check, and never let the Store
# Approval flow pick it as the source for a real Goods Movement.
_NON_USABLE_STOCK_STATUSES = {"inspection", "quality inspection", "blocked", "restricted-use", "restricted", "in transit"}


def is_usable_stock_status(stock_status) -> bool:
    return (stock_status or "").strip().lower() not in _NON_USABLE_STOCK_STATUSES

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


def get_latest_confirmation_by_lot(db, production_lot_ids: list) -> dict:
    """One aggregation, not N queries - same batch pattern used by
    check_component_availability_batch for the STOCK column. User's
    explicit ask: a persistent per-lot indicator for posting/WIP-clearing/
    by-product outcome, since today those only ever show as a toast that
    disappears."""
    if not production_lot_ids:
        return {}
    pipeline = [
        {"$match": {"production_lot_id": {"$in": production_lot_ids}}},
        {"$sort": {"at": -1}},
        {"$group": {"_id": "$production_lot_id", "doc": {"$first": "$$ROOT"}}},
    ]
    result = {}
    for row in db[HISTORY_COLLECTION].aggregate(pipeline):
        d = row["doc"]
        result[row["_id"]] = {
            "success": d.get("success"),
            "wip_clearing": d.get("wip_clearing"),
            "byproduct_confirmation": d.get("byproduct_confirmation"),
            "at": d["at"].isoformat(),
        }
    return result


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
            # inventory_cache stores full site names like "RADISH TECHNOLOGY-P2"
            # (company name + site code), never the bare site code - match on
            # the "-{site_id}" suffix, not exact equality (was always 0 before).
            site_locations = [loc for loc in locations if (loc.get("site") or "").endswith(f"-{site_id}")] if locations is not None else None
            # "Available for production" must be SFG (Semi-Finish Godown)
            # stock only - RM (raw material) hasn't been issued/moved into
            # production yet, so it isn't actually consumable, even though
            # it's on-hand at this site. Bug found (Aug 2026): this summed
            # every warehouse at the site (RM + SFG + Scrap etc.), so a
            # component sitting almost entirely in RM (e.g. 3,995kg RM +
            # 841kg SFG) looked like it had 4,836kg "available" when only
            # 841kg was actually ready to consume - masking real shortages
            # that should have triggered a Store Approval request.
            # Also excludes Quality Inspection/Blocked stock (see
            # is_usable_stock_status above) - that stock sits in the SFG
            # warehouse but isn't actually free to consume yet.
            sfg_locations = [
                loc for loc in (site_locations or [])
                if (loc.get("logistics_area_id") or "").endswith("-SFG") and is_usable_stock_status(loc.get("stock_status"))
            ]
            available_qty = None if site_locations is None else sum(loc["qty"] for loc in sfg_locations)
            components.append({
                "product_id": item["product_id"],
                "description": item.get("description"),
                "unit_of_measure": item.get("unit_of_measure"),
                "required_qty": required_qty,
                "available_qty": available_qty,
                # Per-WAREHOUSE (and stock status, e.g. Unrestricted vs
                # Quality Inspection) breakdown at THIS site only - not
                # other sites, a store user at P2 can't issue from P7's
                # stock anyway. stock_status is included because raw SAP
                # rows can otherwise show what LOOKS like 2 identical
                # "same warehouse" lines with different quantities - they
                # are actually 2 different stock statuses in that warehouse.
                "locations": [
                    {
                        "warehouse": loc.get("logistics_area"), "stock_status": loc.get("stock_status"), "qty": loc["qty"],
                        # SAP Owner Party for this exact stock (e.g. "RI"/"RT") - carried
                        # through so the Store Approval issue flow can auto-fill the
                        # Goods Movement API's owner_party_id from whichever location
                        # the store person actually picks, instead of guessing (Aug 2026).
                        "owner": loc.get("company_code"),
                        # Raw SAP Logistics Area ID (e.g. "P2/P2-RM") - "warehouse"
                        # above is the human-readable description ("RAW MATERIAL
                        # GODOWN-P2"), NOT what the Goods Movement API/rule needs
                        # to match against. Real bug traced to this (Aug 2026):
                        # store approved an issue, RM stock existed, but the fixed
                        # RM->SFG rule compared its ID-format guess against this
                        # description field and never matched, so no movement fired.
                        "warehouse_id": loc.get("logistics_area_id"),
                    }
                    for loc in (site_locations or [])
                ],
                # Needing 0 of a component (e.g. Open Quantity is already 0 -
                # a fully-confirmed row) is never "short", regardless of
                # whether we happen to have on-hand data for it.
                "sufficient": required_qty <= 0 or (available_qty is not None and available_qty >= required_qty),
            })
    return {"checked": True, "reason": None, "components": components}


def check_component_availability(
    db, main_output_product: str, confirmed_quantity: float, site_id: str,
    sap_inventory_client=None, override_bom_id: str = None, sap_soap_client=None,
) -> dict:
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
    glance-view checked on every page load, not a gate before a write.

    `override_bom_id` (Aug 2026, new-order flow only - see
    sap_production_model_client.SAPProductionModelBomClient): when the
    caller already knows exactly which Production Model was picked for
    THIS order via the Source of Supply picker, this is that model's real
    BillOfMaterialID - fetched fresh from SAP (bypassing the cached
    "highest revision" default guess, which is what caused the original
    false-shortage bug) and used for this one check only. Never persisted
    back into bom_node_cache - a one-off, per-order correction, not a
    global cache change. Silently falls back to the cached default doc if
    the live fetch fails or `sap_soap_client` isn't provided, so this is
    purely additive/never blocks the pre-flight check on its own."""
    bom_doc = db["bom_node_cache"].find_one({"_id": main_output_product})
    if override_bom_id and sap_soap_client is not None and override_bom_id != (bom_doc or {}).get("bom_id"):
        try:
            raw = sap_soap_client._fetch_bom_by_id(override_bom_id)
        except Exception as e:
            logger.warning(f"Component availability: live fetch of override BOM '{override_bom_id}' failed, falling back to cached default: {e}")
            raw = None
        if raw and raw.get("groups"):
            bom_doc = {"bom_id": raw["bom_id"], "groups": raw["groups"]}
    stock_by_product = None
    if sap_inventory_client is not None:
        try:
            live_rows = sap_inventory_client.get_inventory_detail()
            stock_by_product = {}
            for row in live_rows:
                stock_by_product.setdefault(row["product_id"], []).append({
                    "site": row.get("site"), "logistics_area": row.get("logistics_area"),
                    "logistics_area_id": row.get("logistics_area_id"),
                    "stock_status": row.get("stock_status"), "qty": row["qty"],
                    "company_code": row.get("company_code"),
                })
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
