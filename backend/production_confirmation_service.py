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

import job_store

logger = logging.getLogger(__name__)

REASON_COLLECTION = "deviation_reason_master"
HISTORY_COLLECTION = "production_confirmation_history"
BOO_ID_CACHE_COLLECTION = "boo_id_cache"
BOO_DESCRIPTIONS_CACHE_COLLECTION = "boo_descriptions_cache"
# Aug 28 2026, user's explicit question before publishing the new Retry
# button ("if proposal created and item A short 10pcs then system created
# a store request... this button visible on this time?"): a "Created"-
# only History row does NOT always mean "stuck/orphaned" - it's also the
# NORMAL, expected look of a row that's still actively being handled,
# either quietly polling in the background or, for a real stock
# shortage, deliberately PAUSED waiting on a human decision on the Store
# Approval page. Retry must never be offered for either of those - only
# for a row whose job has genuinely stopped trying (crashed/errored,
# user-cancelled, gave up after 20 min with no Order ever appearing, or
# its job_store record has since expired past the 24h TTL entirely).
ACTIVE_ORDER_JOB_STATUSES = {
    "running", "checking_stock", "creating_proposal", "waiting_for_order", "releasing_order", "waiting_store_approval",
}


def get_reporting_point_descriptions(db, sap_production_model_bom_client, sap_boo_client, production_model_ids: list) -> dict:
    """{production_model_id: {reporting_point_id: description}} (Aug 2026,
    user's explicit ask - "I NEED REPORTING POINT DESCRIPTION EX:
    BLANK+PUNCH, FLAT-LANCER"). Two-hop lookup, both cached indefinitely
    (a released Bill of Operations' structure essentially never changes):
    Production Model ID -> BillOfOperationsID (OData, ReleasedExecution-
    ProductionModelCollection) -> {ElementID: ElementDescription} (SOAP,
    ReadProductionBillofOperations). A model with no BillOfOperationsID
    (older orders never captured one) or that hits any SAP error is just
    left out of the returned dict - the caller falls back to the raw RP
    code, same as before this feature existed."""
    result = {}
    for model_id in sorted({m for m in production_model_ids if m}):
        cached_boo = db[BOO_ID_CACHE_COLLECTION].find_one({"_id": model_id})
        if cached_boo is not None:
            boo_id = cached_boo.get("bill_of_operations_id")
        else:
            try:
                boo_id = sap_production_model_bom_client.get_bill_of_operations_id_for_model_id(model_id)
            except Exception as e:
                logger.warning(f"Reporting Point descriptions: BillOfOperationsID lookup failed for model {model_id}: {e}")
                boo_id = None
            db[BOO_ID_CACHE_COLLECTION].update_one(
                {"_id": model_id},
                {"$set": {"bill_of_operations_id": boo_id, "cached_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        if not boo_id:
            continue
        cached_desc = db[BOO_DESCRIPTIONS_CACHE_COLLECTION].find_one({"_id": boo_id})
        if cached_desc is not None:
            descriptions = cached_desc.get("descriptions") or {}
        else:
            try:
                descriptions = sap_boo_client.get_marker_element_descriptions(boo_id)
            except Exception as e:
                logger.warning(f"Reporting Point descriptions: SAP lookup failed for BillOfOperationsID {boo_id}: {e}")
                descriptions = {}
            db[BOO_DESCRIPTIONS_CACHE_COLLECTION].update_one(
                {"_id": boo_id},
                {"$set": {"descriptions": descriptions, "cached_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        if descriptions:
            result[model_id] = descriptions
    return result

# Aug 2026: SAP's own stock-status field can carry values like "Inspection"
# (Quality Inspection hold) or "Blocked" - this stock physically sits in
# the warehouse but is NOT free/usable stock. Confirmed live in this
# tenant's real inventory_cache data ("Inspection" appears alongside the
# normal "Not Assigned" = unrestricted status). Never count this stock as
# "available" for a component availability check, and never let the Store
# Approval flow pick it as the source for a real Goods Movement.
_NON_USABLE_STOCK_STATUSES = {"inspection", "quality inspection", "blocked", "restricted-use", "restricted", "in transit"}


def is_usable_stock_status(stock_status, restricted=False) -> bool:
    """`restricted` is SAP's CRESTRICTED_IND flag (see sap_inventory_client.
    get_inventory_detail) - a SEPARATE field from stock_status, not another
    status value. Real incident (Aug 2026): a 1kg lot at site P2 reported
    stock_status "Not Assigned" (normally unrestricted) while ALSO being
    flagged Restricted Use in SAP's own Stock Overview ("Restr." checkbox)
    - every usability check in this app missed it until this was added,
    since none of them looked at anything but stock_status text."""
    return (stock_status or "").strip().lower() not in _NON_USABLE_STOCK_STATUSES and not restricted

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
        "site_id": request_payload.get("site_id"),
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
        "fg_movement": result.get("fg_movement"),
        "at": datetime.now(timezone.utc),
    })


def count_confirmed_today(db, start_utc, end_utc, site_ids) -> int:
    """Aug 25 2026, user's explicit ask - "Confirmed Today" dashboard
    tile. Distinct lots (not raw confirmation events - a lot may be
    confirmed more than once) with a SUCCESSFUL confirmation logged
    within [start_utc, end_utc). site_ids=None means admin/super_admin
    (no restriction); an empty set correctly returns 0 for a "user" with
    no sites bound yet, same fail-closed default as Store Binding."""
    query = {"at": {"$gte": start_utc, "$lt": end_utc}, "success": True}
    if site_ids is not None:
        query["site_id"] = {"$in": list(site_ids)}
    return len(db[HISTORY_COLLECTION].distinct("production_lot_id", query))


def get_scrap_reason_breakdown(db, start_utc, site_ids) -> list:
    """Aug 25 2026, user's explicit ask - "Scrap Trend" dashboard tile:
    total scrap qty + confirmation count grouped by Deviation/Scrap
    Reason code, over the last N days (start_utc). Caller (server.py)
    joins the returned codes against get_deviation_reasons() for labels."""
    match = {"at": {"$gte": start_utc}, "confirmed_scrap": {"$gt": 0}}
    if site_ids is not None:
        match["site_id"] = {"$in": list(site_ids)}
    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$deviation_reason_code", "total_scrap": {"$sum": "$confirmed_scrap"}, "count": {"$sum": 1}}},
        {"$sort": {"total_scrap": -1}},
    ]
    return list(db[HISTORY_COLLECTION].aggregate(pipeline))



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
            "fg_movement": d.get("fg_movement"),
            "at": d["at"].isoformat(),
        }
        for d in docs
    ]


def retry_wip_clearing_for_lot(db, production_lot_id: str, wip_result: dict) -> bool:
    """Overwrites the WIP Clearing outcome on the most recent confirmation
    history doc for this lot ("Retry" button next to a failed "WIP
    Cleared" chip - Aug 2026, user's explicit ask). Returns False if no
    confirmation history exists yet for this lot (shouldn't normally
    happen - a WIP Clearing Run only ever fires after a real confirmation)."""
    latest = db[HISTORY_COLLECTION].find_one({"production_lot_id": production_lot_id}, sort=[("at", -1)])
    if not latest:
        return False
    db[HISTORY_COLLECTION].update_one({"_id": latest["_id"]}, {"$set": {"wip_clearing": wip_result}})
    return True


def get_latest_confirmation_by_lot(db, production_lot_ids: list) -> dict:
    """One aggregation, not N queries - same batch pattern used by
    check_component_availability_batch for the STOCK column. User's
    explicit ask: a persistent per-lot indicator for posting/WIP-clearing/
    by-product outcome, since today those only ever show as a toast that
    disappears.

    Aug 2026 fix: now that a single lot can have multiple independent
    Reporting Points (e.g. RP10/RP20/END on a multi-op model), grouping
    by production_lot_id alone made every row of that lot show the SAME
    "Posted"/"By-product" badge - including RPs that were never actually
    confirmed. Key is now `{lot}::{reporting_point_id}` so each Reporting
    Point only shows its own outcome."""
    if not production_lot_ids:
        return {}
    pipeline = [
        {"$match": {"production_lot_id": {"$in": production_lot_ids}}},
        {"$sort": {"at": -1}},
        {"$group": {"_id": {"lot": "$production_lot_id", "rp": "$reporting_point_id"}, "doc": {"$first": "$$ROOT"}}},
    ]
    result = {}
    for row in db[HISTORY_COLLECTION].aggregate(pipeline):
        # Aug 2026 fix: a doc from before reporting_point_id existed on this
        # schema has no "rp" key in the grouped _id at all (Mongo omits a
        # missing-field group key entirely rather than nulling it) - used to
        # KeyError and 500 the WHOLE batch, hiding every other lot's badges
        # too. Such a legacy doc can never match any current row's key
        # anyway, so just skip it instead of crashing.
        if "rp" not in row["_id"]:
            continue
        d = row["doc"]
        key = f"{row['_id']['lot']}::{row['_id']['rp']}"
        result[key] = {
            "success": d.get("success"),
            "wip_clearing": d.get("wip_clearing"),
            "byproduct_confirmation": d.get("byproduct_confirmation"),
            # Aug 27 2026, user's explicit ask: FG Goods Movement outcome +
            # the confirmed_quantity a "Retry" button needs to re-post the
            # exact same quantity (site_id/unit_code/main_output_product
            # are already on the open-lots row itself, no need to also
            # carry them here).
            "fg_movement": d.get("fg_movement"),
            "confirmed_quantity": d.get("confirmed_quantity"),
            # Aug 25 2026, user's explicit ask: show whether that specific
            # confirmation was Partial or Full for this Reporting Point.
            "confirmation_finished": d.get("confirmation_finished"),
            "at": d["at"].isoformat(),
        }
    return result


def get_or_classify_category(db, product_id: str):
    """Prefers the already-tagged component_master category (rule OR
    manual - never overwrites either); falls back to a live, single-
    product classification (bom_categorizer.classify_single_product_live)
    and persists it, for an item that was never run through the
    Inventory page's categorizer at all. Returns (category|None,
    was_live_checked: bool)."""
    import bom_categorizer
    doc = db["component_master"].find_one({"_id": product_id}, {"category": 1})
    cached = (doc or {}).get("category")
    if cached:
        return cached, False
    live_category = bom_categorizer.classify_single_product_live(db, product_id)
    if live_category:
        db["component_master"].update_one(
            {"_id": product_id},
            {"$set": {"category": live_category, "category_source": "rule", "categorized_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    return live_category, True


def retry_fg_movement_for_lot(db, production_lot_id: str, fg_movement_result: dict) -> bool:
    """Overwrites the FG Goods Movement outcome on the most recent
    confirmation history doc for this lot ("Retry" button next to a
    failed "FG Moved" chip - Aug 27 2026, user's explicit ask). Returns
    False if no confirmation history exists yet for this lot."""
    latest = db[HISTORY_COLLECTION].find_one({"production_lot_id": production_lot_id}, sort=[("at", -1)])
    if not latest:
        return False
    db[HISTORY_COLLECTION].update_one({"_id": latest["_id"]}, {"$set": {"fg_movement": fg_movement_result}})
    return True


PROPOSAL_HISTORY_COLLECTION = "production_order_creation_history"


def log_proposal_creation(db, actor: str, request_payload: dict, result: dict, job_id: str = None, actor_user_id: str = None) -> None:
    """job_id (when this came from the one-click create-and-release job, not
    the standalone create-proposal-only endpoint) is stored so a later
    log_order_release call for the SAME job can update this exact row
    in-place instead of appearing as a disconnected second row - lets the
    history table show "Proposal 223835 -> Order 69959" together.

    actor_user_id (Aug 28 2026 bug fix - real incident, Mayank Jadon's own
    proposal invisible on his own Recent Activity table) - the STABLE
    Entra ID identity (tid:oid, from request.state.user["_id"]) of
    whoever is logged in right now, stored ALONGSIDE the free-text
    `actor` display name. "Mine" filtering (get_proposal_and_release_history
    below, and get_order_creators for the open-lots table) now matches on
    THIS instead of the display name string, which can silently drift
    (Azure AD name claim changes, trailing/internal whitespace, etc.) even
    though it's the exact same person - `actor` itself is kept purely for
    display, never used for access decisions anymore."""
    db[PROPOSAL_HISTORY_COLLECTION].insert_one({
        "type": "proposal_created",
        "job_id": job_id,
        "actor": actor,
        "actor_user_id": actor_user_id,
        "material_id": request_payload.get("material_id"),
        "site_id": request_payload.get("site_id"),
        "quantity": request_payload.get("quantity"),
        "unit_code": request_payload.get("unit_code"),
        "production_proposal_id": result.get("production_proposal_id"),
        # Aug 2026, user's explicit ask: which Production Model (Source of
        # Supply) was picked at creation time - can only ever be set here,
        # never retroactively, since it's a one-time creation choice.
        "production_model_id": request_payload.get("production_model_id"),
        "at": datetime.now(timezone.utc),
    })


def log_order_release(db, actor: str, production_order_id: str, result: dict, job_id: str = None, actor_user_id: str = None, match_proposal_id: str = None) -> None:
    """When job_id matches the same one-click job's proposal_created row,
    update that row in-place with the order outcome instead of inserting a
    disconnected second row. Falls back to a standalone insert (previous
    behavior) for the manual/standalone Release-an-existing-Order form,
    which has no job_id. actor_user_id: see log_proposal_creation above.
    match_proposal_id (Aug 28 2026, "Retry" from an old Created-only
    History row - see retry_from_proposal below): a fresh retry runs
    under a BRAND NEW job_id (the original job doc has long since expired
    from job_store's 24h TTL, or might even still be alive as a DIFFERENT
    unrelated job) - matching by the Proposal ID itself instead still
    finds and updates the SAME original row in-place."""
    if job_id or match_proposal_id:
        query = {"job_id": job_id, "type": "proposal_created"} if job_id else {"production_proposal_id": match_proposal_id, "type": "proposal_created"}
        updated = db[PROPOSAL_HISTORY_COLLECTION].update_one(
            query,
            {"$set": {
                "production_order_id": production_order_id,
                "released": result.get("success"),
                "released_at": datetime.now(timezone.utc),
                "released_by": actor,
                "released_by_user_id": actor_user_id,
            }},
        )
        if updated.matched_count:
            return
    db[PROPOSAL_HISTORY_COLLECTION].insert_one({
        "type": "order_released",
        "actor": actor,
        "actor_user_id": actor_user_id,
        "production_order_id": production_order_id,
        "success": result.get("success"),
        "at": datetime.now(timezone.utc),
    })


def get_proposal_and_release_history(db, limit: int = 200) -> list:
    """can_retry (Aug 28 2026): only true for a "Created"-only row whose
    job has genuinely stopped trying - see ACTIVE_ORDER_JOB_STATUSES
    above for exactly why "still actively polling" and "paused on a
    Store Approval decision" are both excluded, not just "job doc
    expired". Looked up in one batched query, not per-row."""
    docs = list(db[PROPOSAL_HISTORY_COLLECTION].find({}).sort("at", -1).limit(limit))
    pending_job_ids = [d["job_id"] for d in docs if d.get("type") == "proposal_created" and not d.get("production_order_id") and d.get("job_id")]
    active_job_ids = set()
    if pending_job_ids:
        for j in db[job_store.COLLECTION_NAME].find({"_id": {"$in": pending_job_ids}}, {"status": 1}):
            if j.get("status") in ACTIVE_ORDER_JOB_STATUSES:
                active_job_ids.add(j["_id"])
    return [
        {
            "type": d.get("type"),
            "actor": d.get("actor"),
            "actor_user_id": d.get("actor_user_id"),
            "material_id": d.get("material_id"),
            "site_id": d.get("site_id"),
            "quantity": d.get("quantity"),
            "unit_code": d.get("unit_code"),
            "production_proposal_id": d.get("production_proposal_id"),
            "production_order_id": d.get("production_order_id"),
            "production_model_id": d.get("production_model_id"),
            "released": d.get("released"),
            "released_by": d.get("released_by"),
            "released_by_user_id": d.get("released_by_user_id"),
            "success": d.get("success"),
            "at": d["at"].isoformat(),
            "can_retry": d.get("type") == "proposal_created" and not d.get("production_order_id") and d.get("job_id") not in active_job_ids,
        }
        for d in docs
    ]


def get_order_creators(db, production_order_ids: list) -> dict:
    """{production_order_id: {"name": actor_name, "user_id": str|None,
    "at": datetime, "production_model_id": str|None}} for the "Show
    mine"/"Sort by latest" controls (user's explicit ask, Aug 2026) on the
    Production Confirmation table - "mine" means orders I created/
    released, not orders I've merely confirmed. Prefers `released_by`
    (the merged proposal_created row, the normal one-click flow) over
    `actor` (the standalone manual Release-an-Order form, which has no
    linked proposal row) - same preference applied to `user_id` (Aug 28
    2026, see log_proposal_creation's docstring for why). `at` is the
    order-creation timestamp, used so "latest" reflects when the order
    was actually created rather than SAP's own (unexposed) lot ordering.
    `production_model_id` (Aug 27 2026, user's explicit ask - "which
    Production Model did I use to create this order?") is only ever
    known for orders created via the Source of Supply picker after this
    fix - older orders show None, the frontend renders that as "—"."""
    if not production_order_ids:
        return {}
    docs = db[PROPOSAL_HISTORY_COLLECTION].find(
        {"production_order_id": {"$in": production_order_ids}},
        {"production_order_id": 1, "released_by": 1, "actor": 1, "released_by_user_id": 1, "actor_user_id": 1,
         "at": 1, "released_at": 1, "production_model_id": 1},
    ).sort("_id", 1)
    creators = {}
    for d in docs:
        order_id = d.get("production_order_id")
        if order_id:
            creators[order_id] = {
                "name": d.get("released_by") or d.get("actor"),
                "user_id": d.get("released_by_user_id") or d.get("actor_user_id"),
                "at": d.get("released_at") or d.get("at"),
                "production_model_id": d.get("production_model_id"),
            }
    return creators




def load_stock_by_product(db) -> dict:
    """Loads the current inventory_cache (refreshed every 30 min from live
    SAP, see server.py's start_inventory_cache_refresh_loop) into a
    {product_id: [raw locations]} shape - shared by the shortage check
    below (via check_component_availability_batch) and store_approval_
    service.refresh_component_locations (re-syncing an ALREADY-OPEN
    request's stock display/movement-source against however fresh the
    cache now is, instead of the frozen snapshot from whenever the
    request was first created - real incident, Aug 2026: a Goods Receipt
    posted an hour earlier had already reached inventory_cache, but an
    already-open Store Approval request still showed the old, lower RM
    quantity and would have skipped the movement entirely)."""
    inventory_doc = db["inventory_cache"].find_one({"_id": "latest"})
    stock_by_product = {}
    for item in (inventory_doc or {}).get("items", []):
        stock_by_product[item["product_id"]] = item.get("locations", [])
    return stock_by_product


def apply_goods_movement_to_cache(db, product_id: str, source_warehouse_id: str, target_warehouse_id: str, qty: float) -> None:
    """Aug 2026 - user feedback: "if the movement was successful, the app
    must update its cache so production confirmation can continue" (the
    Stock/Short badge was still showing pre-movement quantities for up to
    30 min after a genuinely successful Store Approval issue, since it
    only ever reads the scheduled/cache-only inventory_cache - see
    load_stock_by_product above). Rather than trigger a fresh live SAP
    pull (tried and explicitly reverted in an earlier session - it made
    the UI look "stuck" for minutes whenever this flaky tenant's cost
    lookups were slow), this surgically patches the SAME inventory_cache
    document with a movement we OURSELVES just confirmed SAP accepted -
    no new SAP call at all, so it can never hang or fail. Decrements the
    source (RM) location's qty and increments (or creates) the target
    (SFG) location's qty for this exact product only. A no-op (safe,
    silent) if this product/location isn't in the cache yet - the next
    scheduled refresh will pick it up normally."""
    if not qty or qty <= 0:
        return
    doc = db["inventory_cache"].find_one({"_id": "latest"}, {"items": 1})
    if not doc:
        return
    items = doc.get("items", [])
    item = next((it for it in items if it.get("product_id") == product_id), None)
    if item is None:
        return
    locations = item.setdefault("locations", [])
    source_loc = next(
        (loc for loc in locations if loc.get("logistics_area_id") == source_warehouse_id and is_usable_stock_status(loc.get("stock_status"), loc.get("restricted"))),
        None,
    )
    if source_loc:
        source_loc["qty"] = round((source_loc.get("qty") or 0) - qty, 4)
    target_loc = next(
        (loc for loc in locations if loc.get("logistics_area_id") == target_warehouse_id and is_usable_stock_status(loc.get("stock_status"), loc.get("restricted"))),
        None,
    )
    if target_loc:
        target_loc["qty"] = round((target_loc.get("qty") or 0) + qty, 4)
    else:
        template = source_loc or (locations[0] if locations else {})
        locations.append({
            "site": template.get("site"), "logistics_area": target_warehouse_id.split("/")[-1],
            "logistics_area_id": target_warehouse_id, "stock_status": "Not Assigned", "restricted": False, "qty": qty,
            "company_code": template.get("company_code"), "company_name": template.get("company_name"),
        })
    item["total_qty"] = round(sum(loc.get("qty") or 0 for loc in locations), 4)
    db["inventory_cache"].update_one({"_id": "latest"}, {"$set": {"items": items}})


def site_locations_for_product(stock_by_product: dict, product_id: str, site_id: str):
    """Returns this product's locations at `site_id`, in the app's
    standard per-component display/movement-source shape (warehouse,
    stock_status, restricted, qty, owner, warehouse_id), or None if the
    product has no cache entry at all (distinct from an empty list, which
    means "cached, but zero stock at this site"). inventory_cache stores
    full site names like "RADISH TECHNOLOGY-P2" (company name + site
    code), never the bare site code - match on the "-{site_id}" suffix,
    not exact equality (was always 0 before)."""
    locations = stock_by_product.get(product_id)
    if locations is None:
        return None
    site_locations = [loc for loc in locations if (loc.get("site") or "").endswith(f"-{site_id}")]
    return [
        {
            "warehouse": loc.get("logistics_area"), "stock_status": loc.get("stock_status"), "qty": loc["qty"],
            # SAP's CRESTRICTED_IND flag - a SEPARATE field from
            # stock_status (see is_usable_stock_status docstring). Carried
            # through so both the Store Approval/Production Confirmation
            # displays AND the actual movement-matching logic below treat
            # this stock as not usable, even when stock_status itself
            # still reads a normal "Not Assigned".
            "restricted": loc.get("restricted", False),
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
        for loc in site_locations
    ]


def _check_availability_against_stock(bom_doc: dict, stock_by_product: dict, confirmed_quantity: float, site_id: str, sub_assembly_ids: frozenset = frozenset()) -> dict:
    if not bom_doc or not bom_doc.get("groups"):
        return {"checked": False, "reason": "No cached BOM found locally for this product - cannot check component availability.", "components": []}
    components = []
    for group in bom_doc["groups"]:
        for item in group["items"]:
            if not item.get("active") or item.get("quantity") is None:
                continue
            required_qty = round(item["quantity"] * confirmed_quantity, 4)
            # Per-WAREHOUSE (and stock status, e.g. Unrestricted vs Quality
            # Inspection) breakdown at THIS site only - not other sites, a
            # store user at P2 can't issue from P7's stock anyway.
            site_locations = site_locations_for_product(stock_by_product, item["product_id"], site_id)
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
                if (loc.get("warehouse_id") or "").endswith("-SFG") and is_usable_stock_status(loc.get("stock_status"), loc.get("restricted"))
            ]
            available_qty = None if site_locations is None else sum(loc["qty"] for loc in sfg_locations)
            components.append({
                "product_id": item["product_id"],
                "description": item.get("description"),
                "unit_of_measure": item.get("unit_of_measure"),
                "required_qty": required_qty,
                "available_qty": available_qty,
                # Per-WAREHOUSE (and stock status, e.g. Unrestricted vs
                # Quality Inspection) breakdown at THIS site only - already
                # in final shape via site_locations_for_product() above.
                "locations": site_locations or [],
                # Needing 0 of a component (e.g. Open Quantity is already 0 -
                # a fully-confirmed row) is never "short", regardless of
                # whether we happen to have on-hand data for it.
                "sufficient": required_qty <= 0 or (available_qty is not None and available_qty >= required_qty),
                # Aug 2026 bug fix: a short component that is itself a
                # manufactured Sub-Assembly (has its own cached BOM) is NOT
                # something the physical Store can "issue" - its stock only
                # exists once ITS OWN production order is confirmed. The
                # order-creation flow uses this flag to block with a clear
                # error instead of wrongly opening a Store Approval request
                # for it (see _run_create_and_release_job in server.py).
                "is_sub_assembly": item["product_id"] in sub_assembly_ids,
            })
    return {"checked": True, "reason": None, "components": components}


def _desc_by_product_from_cache(db, product_ids: frozenset = None) -> dict:
    """Best-effort product_id -> description lookup from inventory_cache
    (the only local collection that reliably carries descriptions for ANY
    product, including ones like FLAT-BK21 that never appear in any
    cached BOM at all)."""
    inventory_doc = db["inventory_cache"].find_one({"_id": "latest"})
    desc = {}
    for item in (inventory_doc or {}).get("items", []):
        if product_ids is None or item["product_id"] in product_ids:
            desc[item["product_id"]] = item.get("description")
    return desc


def _check_availability_against_material_inputs(
    material_inputs: list, stock_by_product: dict, desc_by_product: dict, confirmed_quantity: float, site_id: str, sub_assembly_ids: frozenset = frozenset(),
) -> dict:
    """Same shape/logic as _check_availability_against_stock, but sourced
    from this lot's own exact SAP MaterialInput list (see
    check_component_availability_from_material_inputs) instead of a
    guessed cached BOM."""
    if not material_inputs:
        return {"checked": False, "reason": "This lot has no planned components (MaterialInput) in SAP - cannot check component availability.", "components": []}
    components = []
    for mi in material_inputs:
        product_id = mi.get("product_id")
        if not product_id or mi.get("qty_per_unit") is None:
            continue
        required_qty = round(mi["qty_per_unit"] * confirmed_quantity, 4)
        site_locations = site_locations_for_product(stock_by_product, product_id, site_id)
        sfg_locations = [
            loc for loc in (site_locations or [])
            if (loc.get("warehouse_id") or "").endswith("-SFG") and is_usable_stock_status(loc.get("stock_status"), loc.get("restricted"))
        ]
        available_qty = None if site_locations is None else sum(loc["qty"] for loc in sfg_locations)
        components.append({
            "product_id": product_id,
            "description": desc_by_product.get(product_id),
            "unit_of_measure": mi.get("unit_code"),
            "required_qty": required_qty,
            "available_qty": available_qty,
            "locations": site_locations or [],
            "sufficient": required_qty <= 0 or (available_qty is not None and available_qty >= required_qty),
            "is_sub_assembly": product_id in sub_assembly_ids,
        })
    return {"checked": True, "reason": None, "components": components}


def check_component_availability_from_material_inputs(
    db, material_inputs: list, confirmed_quantity: float, site_id: str, sap_inventory_client=None, sfg_only: bool = False,
) -> dict:
    """Aug 27 2026 fix for the mis-diagnosed "wrong Production Model"
    shortage bug: `check_component_availability` had to GUESS which of a
    product's several active SAP Production Models applies (cached
    "highest revision" default, or a site-scoped Source-of-Supply
    lookup that still needs 0/1/2+ disambiguation) - but each Production
    Lot returned by SAPProductionLotClient already carries its own exact,
    already-resolved MaterialInput list (the real components SAP itself
    planned this specific lot against, e.g. FLAT-BK21 for lot 70411 at
    P2, never SH4.5HR). This bypasses BOM resolution entirely - no
    guessing, no ambiguity, always correct for any lot that has already
    been planned/released in SAP. Same live-stock-first/cache-fallback
    behavior as check_component_availability."""
    product_ids = [mi["product_id"] for mi in material_inputs if mi.get("product_id")]
    stock_by_product = None
    desc_by_product = {}
    if sap_inventory_client is not None:
        try:
            if sfg_only:
                live_rows = sap_inventory_client.get_inventory_detail(warehouse_ids=[f"{site_id}/{site_id}-SFG"])
            else:
                live_rows = sap_inventory_client.get_inventory_detail(site_id=site_id)
            stock_by_product = {}
            for row in live_rows:
                stock_by_product.setdefault(row["product_id"], []).append({
                    "site": row.get("site"), "logistics_area": row.get("logistics_area"),
                    "logistics_area_id": row.get("logistics_area_id"),
                    "stock_status": row.get("stock_status"), "restricted": row.get("restricted", False), "qty": row["qty"],
                    "company_code": row.get("company_code"),
                })
                desc_by_product.setdefault(row["product_id"], row.get("description"))
        except Exception:
            stock_by_product = None  # fall through to cache below
    if stock_by_product is None:
        stock_by_product = load_stock_by_product(db)
    if product_ids and any(pid not in desc_by_product for pid in product_ids):
        desc_by_product.update({k: v for k, v in _desc_by_product_from_cache(db, frozenset(product_ids)).items() if k not in desc_by_product})
    sub_assembly_ids = frozenset(
        d["_id"] for d in db["bom_node_cache"].find({"_id": {"$in": product_ids}, "groups": {"$ne": []}}, {"_id": 1})
    ) if product_ids else frozenset()
    return _check_availability_against_material_inputs(material_inputs, stock_by_product, desc_by_product, confirmed_quantity, site_id, sub_assembly_ids)


def _resolve_bom_doc(db, main_output_product: str, override_bom_id: str = None, sap_soap_client=None) -> dict:
    """Shared by check_component_availability and get_bom_stock_status -
    see override_bom_id's docstring on check_component_availability."""
    bom_doc = db["bom_node_cache"].find_one({"_id": main_output_product})
    if override_bom_id and sap_soap_client is not None and override_bom_id != (bom_doc or {}).get("bom_id"):
        try:
            raw = sap_soap_client._fetch_bom_by_id(override_bom_id)
        except Exception as e:
            logger.warning(f"Component availability: live fetch of override BOM '{override_bom_id}' failed, falling back to cached default: {e}")
            raw = None
        if raw and raw.get("groups"):
            bom_doc = {"bom_id": raw["bom_id"], "groups": raw["groups"]}
    return bom_doc


def check_component_availability(
    db, main_output_product: str, confirmed_quantity: float, site_id: str,
    sap_inventory_client=None, override_bom_id: str = None, sap_soap_client=None,
    sfg_only: bool = False, sap_production_model_client=None, sap_production_model_bom_client=None,
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

    `sfg_only` (Aug 2026, order-creation pre-flight only): "sufficient" was
    ALWAYS SFG-only (see _check_availability_against_stock - RM/QC are
    only ever used for display, never for the sufficiency verdict itself).
    This flag just also scopes the LIVE SAP PULL down to only the SFG
    warehouse at this site (verified live even faster than a whole-site
    pull) - safe because the order-creation caller only needs the
    sufficient/short verdict, not a full RM/SFG/QC display breakdown (any
    component that ends up short gets its RM/QC locations correctly
    refreshed anyway the first time anyone views the resulting Store
    Approval request - see store_approval_service.refresh_component_
    locations). The Confirm dialog's live availability panel does want
    that full breakdown for display, so it leaves this False (default).

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
    purely additive/never blocks the pre-flight check on its own.

    `sap_production_model_client`/`sap_production_model_bom_client` (Aug
    27 2026, user's explicit bug report - existing-lot Production
    Confirmation for BK-0021 at P2 showed a shortage against "SH4.5HR",
    but SAP's real released Production Model for P2 (BK-0021_1) uses
    "FLAT-BK21" - a DIFFERENT, also-Released Production Model (BK-0021_2)
    for a different site uses SH4.5HR, and the cached "highest revision"
    guess had no way to know which one this lot's site actually needs).
    When `override_bom_id` isn't already given, and both these clients
    are provided, auto-resolves it: looks up this material's Source of
    Supply options scoped to THIS site - if there's exactly one
    unambiguous Production Model valid for this site, uses its real
    locked BillOfMaterialID instead of the cached guess. Any ambiguity
    (0 or 2+ site-scoped models) or lookup failure silently falls back to
    the old cached-default behavior - never blocks the check on its own."""
    if not override_bom_id and sap_production_model_client is not None and sap_production_model_bom_client is not None:
        try:
            product_uuid = (db["component_master"].find_one({"_id": main_output_product}, {"product_uuid": 1}) or {}).get("product_uuid")
            if product_uuid:
                options = sap_production_model_client.get_source_of_supply_options(product_uuid, site_id)
                if len(options) == 1:
                    override_bom_id = sap_production_model_bom_client.get_bill_of_material_id_for_model(options[0]["production_model_uuid"])
        except Exception as e:
            logger.warning(f"Component availability: site-scoped Production Model auto-resolve failed for '{main_output_product}' at {site_id}, using cached default instead: {e}")
    bom_doc = _resolve_bom_doc(db, main_output_product, override_bom_id, sap_soap_client)
    stock_by_product = None
    if sap_inventory_client is not None:
        try:
            if sfg_only:
                live_rows = sap_inventory_client.get_inventory_detail(warehouse_ids=[f"{site_id}/{site_id}-SFG"])
            else:
                live_rows = sap_inventory_client.get_inventory_detail(site_id=site_id)
            stock_by_product = {}
            for row in live_rows:
                stock_by_product.setdefault(row["product_id"], []).append({
                    "site": row.get("site"), "logistics_area": row.get("logistics_area"),
                    "logistics_area_id": row.get("logistics_area_id"),
                    "stock_status": row.get("stock_status"), "restricted": row.get("restricted", False), "qty": row["qty"],
                    "company_code": row.get("company_code"),
                })
        except Exception:
            stock_by_product = None  # fall through to cache below
    if stock_by_product is None:
        stock_by_product = load_stock_by_product(db)
    # Aug 2026 bug fix: which of THIS BOM's own components are themselves
    # manufactured Sub-Assemblies (have their own cached BOM) rather than
    # a pure RM/bought-out leaf part - see is_sub_assembly above.
    component_ids = [item["product_id"] for group in (bom_doc or {}).get("groups", []) for item in group["items"]]
    sub_assembly_ids = frozenset(
        d["_id"] for d in db["bom_node_cache"].find({"_id": {"$in": component_ids}, "groups": {"$ne": []}}, {"_id": 1})
    ) if component_ids else frozenset()
    return _check_availability_against_stock(bom_doc, stock_by_product, confirmed_quantity, site_id, sub_assembly_ids)


def get_bom_stock_status(db, main_output_product: str, override_bom_id: str = None, sap_inventory_client=None, sap_soap_client=None) -> dict:
    """Holistic (every site/warehouse, NOT just one order's own site) BOM
    component stock status - Aug 2026, user's explicit ask: once a Source
    of Supply (Production Model) is picked for a new order, show where
    EACH of its BOM's components (RM and SFG alike, item-level) actually
    sits across the whole company, informationally - a different view
    from check_component_availability's single-site sufficiency verdict
    above (this never blocks anything). `sap_inventory_client=None` (the
    picker's default, on model selection) stays cache-only for an instant
    first render; passed in (the page's "Check Live Stock" button) it
    pulls this BOM's exact components straight from SAP via product_ids
    (fast - filtered directly on CMATERIAL_UUID, see sap_inventory_client's
    docstring) and silently falls back to the cache if that live pull
    fails, same fallback pattern as check_component_availability."""
    bom_doc = _resolve_bom_doc(db, main_output_product, override_bom_id, sap_soap_client)
    if not bom_doc or not bom_doc.get("groups"):
        return {"checked": False, "reason": "No cached BOM found locally for this product - cannot check component stock status.", "components": [], "source": None, "fetched_at": None}

    component_meta = {}
    for group in bom_doc["groups"]:
        for item in group["items"]:
            if not item.get("active") or item.get("quantity") is None:
                continue
            component_meta[item["product_id"]] = {
                "description": item.get("description"),
                "unit_of_measure": item.get("unit_of_measure"),
                # Per-unit-of-main-output BOM ratio - the frontend panel
                # multiplies this by whatever order Quantity the planner has
                # typed (live, no extra call needed as they adjust it) to
                # show Required qty and the resulting Shortfall, same ratio
                # check_component_availability itself uses server-side.
                "bom_qty_per_unit": item["quantity"],
            }
    if not component_meta:
        return {"checked": True, "reason": None, "components": [], "source": None, "fetched_at": None}
    product_ids = list(component_meta.keys())

    source, fetched_at, stock_by_product = None, None, {}
    if sap_inventory_client is not None:
        try:
            for row in sap_inventory_client.get_inventory_detail(product_ids=product_ids):
                stock_by_product.setdefault(row["product_id"], []).append(row)
            source, fetched_at = "live", datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logger.warning(f"BOM stock status: live SAP pull for {len(product_ids)} component(s) of '{main_output_product}' failed, falling back to cache: {e}")
    if source is None:
        cache_stock = load_stock_by_product(db)
        for pid in product_ids:
            if pid in cache_stock:
                stock_by_product[pid] = cache_stock[pid]
        source = "cache"
        cache_doc = db["inventory_cache"].find_one({"_id": "latest"}, {"updated_at": 1})
        fetched_at = cache_doc["updated_at"].isoformat() if cache_doc and cache_doc.get("updated_at") else None

    components = []
    for product_id, meta in component_meta.items():
        locations = [
            {
                "site": loc.get("site"),
                "warehouse": loc.get("logistics_area"),
                "warehouse_id": loc.get("logistics_area_id"),
                "stock_status": loc.get("stock_status"),
                "restricted": loc.get("restricted", False),
                "qty": loc.get("qty", 0),
            }
            for loc in stock_by_product.get(product_id, [])
        ]
        total_usable_qty = round(sum(loc["qty"] for loc in locations if is_usable_stock_status(loc["stock_status"], loc["restricted"])), 4)
        components.append({
            "product_id": product_id,
            "description": meta["description"],
            "unit_of_measure": meta["unit_of_measure"],
            "bom_qty_per_unit": meta["bom_qty_per_unit"],
            "total_usable_qty": total_usable_qty,
            "locations": locations,
        })
    return {"checked": True, "reason": None, "components": components, "source": source, "fetched_at": fetched_at}


def check_component_availability_batch(db, rows: list) -> list:
    """Batch counterpart of check_component_availability() for the open-lots
    list - fetches inventory_cache and every needed bom_node_cache doc ONCE
    (not once per row) so a 100+ row list stays instant. `rows` is a list
    of {main_output_product, quantity, site_id, material_inputs}; returns a
    same-length list of {checked, reason, sufficient_all, short_components}
    - a compact summary (not the full component list) since the list view
    only needs a badge + the short ones for a tooltip, not every sufficient
    component.

    Aug 27 2026: when a row carries `material_inputs` (this lot's own exact
    SAP MaterialInput list, from SAPProductionLotClient - see
    check_component_availability_from_material_inputs), that is used
    directly instead of guessing a BOM by main_output_product alone - the
    same "wrong Production Model" bug this list's badges had too. Rows
    without it (e.g. lots SAP returned with no planned MaterialInput yet)
    fall back to the old cached-BOM behavior unchanged."""
    stock_by_product = load_stock_by_product(db)

    bom_needed_ids = {r["main_output_product"] for r in rows if r.get("main_output_product") and not r.get("material_inputs")}
    bom_docs = {d["_id"]: d for d in db["bom_node_cache"].find({"_id": {"$in": list(bom_needed_ids)}})} if bom_needed_ids else {}

    mi_product_ids = frozenset(mi["product_id"] for r in rows for mi in (r.get("material_inputs") or []) if mi.get("product_id"))
    desc_by_product = _desc_by_product_from_cache(db, mi_product_ids) if mi_product_ids else {}
    sub_assembly_ids = frozenset(
        d["_id"] for d in db["bom_node_cache"].find({"_id": {"$in": list(mi_product_ids)}, "groups": {"$ne": []}}, {"_id": 1})
    ) if mi_product_ids else frozenset()

    results = []
    for r in rows:
        material_inputs = r.get("material_inputs")
        if material_inputs:
            result = _check_availability_against_material_inputs(
                material_inputs, stock_by_product, desc_by_product, r.get("quantity") or 0, r.get("site_id"), sub_assembly_ids,
            )
        else:
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
