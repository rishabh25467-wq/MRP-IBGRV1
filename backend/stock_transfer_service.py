"""SAP Business ByDesign Inter-Plant Stock Transfer Order (STO) screen
backend (Aug 2026).

Real SAP write happens via a specific web service, "ManageCustomerRequirementIn"
(Customer Requirement Processing process component) - confirmed via SAP's own
public docs, header fields ShipFromSiteID/ShipToSiteID/ShipToLocationID with
line items under ExternalRquestItem. The live write (see submit_order_to_sap
below + sap_sto_client.py) is wired in as of Aug 27 2026 - every order still
gets created here LOCALLY FIRST (status "pending_sap"), then a background
job immediately submits it live to SAP (Check first as a safety net, then
the real Maintain write - user's explicit "go live immediately" ask, no
separate dry-run mode), flipping status to "created_in_sap"/"sap_failed".

Reuses the SAME Site -> Company mapping already used for WIP Clearing
(sap_wip_clearing_client.SITE_TO_COMPANY) for the "Ship-to Site must be in
the same Company as Ship-from Site" rule - the user explicitly confirmed
(Aug 2026) that mapping is correct for this feature too, over the spec's own
illustrative (and not accurate for this tenant) example.

Full Goods Issue automation (Aug 27 2026, user's explicit ask - "we need
full", fully backend, user should never have to see/do anything extra):
once SAP converts this order's Customer Requirement into an Outbound
Delivery Request (SAP's own internal scheduling - not something this app
triggers, polled for by find_ready_outbound_delivery_item() below), the
Goods Issue is posted via a custom OData service (`odataoutboundemergent`,
sap_outbound_delivery_client.py) that ALREADY EXISTS on this tenant (built
for a sister app's different use case, reused here) - see that module's
docstring for how the Customer-Requirement -> Outbound-Delivery-Request
link is safely made (UUID match, zero collision risk) and how the single
PGIInBackground call posts + releases the Goods Issue in one shot. Runs as
its own independent background loop (see server.py's _run_goods_issue_job)
kicked off right after submit_order_to_sap() succeeds - NOT part of the
user-facing progress dialog (that one only covers Validate/Check/Create,
which finish in seconds; this can take much longer since it's waiting on
SAP's own scheduling), purely reflected via the STO doc's own
`gi_status`/`gi_error`/`outbound_delivery_object_id` fields, same as any
other field the detail modal already reads."""
import json
import logging
import re
import uuid
from datetime import datetime, timezone, date

from pymongo import ReturnDocument

from sap_wip_clearing_client import company_and_set_of_books_for_site
from production_confirmation_service import is_usable_stock_status
from inventory_service import list_known_sites
from sap_sto_client import SAPSTOError
from sap_outbound_delivery_client import SAPOutboundDeliveryError
import job_store

logger = logging.getLogger(__name__)

STO_COLLECTION = "stock_transfer_orders"
INVENTORY_CACHE_COLLECTION = "inventory_cache"


def _site_id_from_full_site(full_site: str) -> str:
    """inventory_cache's `site` field is "{COMPANY DISPLAY NAME}-{SITE_ID}"
    (e.g. "RAY INTERNATIONAL-P1", "RADISH TECHNOLOGIES-P2W") - the real
    site ID is just the suffix after the last "-", same split
    inventory_service.list_known_sites already uses."""
    return (full_site or "").rsplit("-", 1)[-1].strip().upper()


def _warehouse_id_from_logistics_area_id(logistics_area_id: str) -> str:
    """SAP's real Logistics Area ID is just "{SITE}-{TYPE}" (e.g. "P2-RM")
    - this app's own inventory report data prefixes it with "{site}/" as a
    display-key (e.g. "P2/P2-RM"), same normalization
    sap_goods_movement_client._normalize_logistics_area_id already does."""
    return (logistics_area_id or "").rsplit("/", 1)[-1].strip()


def get_product_stock_locations(db, product_id: str, include_non_usable: bool = False) -> dict:
    """Every in-stock location this product currently sits in, across
    every site/warehouse - backs both the "Check Inventory" step and the
    "Select Source Warehouse" dropdown. Purely cache-based
    (inventory_cache, refreshed every couple hours - the same source
    InventoryPage.js already reads), no live SAP call - this is a fast,
    frequent lookup as the user searches/picks items, not a one-off
    pre-write sufficiency gate like check_component_availability.

    By default (`include_non_usable=False`, every existing caller -
    suggestion/validation/creation) only USABLE stock is returned, same
    as before. `include_non_usable=True` (Aug 27 2026, user's explicit
    ask - "show Inspection/Restricted stock in the dropdown as read-only
    info") additionally includes non-usable rows (Inspection/Blocked/
    Restricted), each tagged `is_usable: False` - the frontend renders
    these as visible-but-disabled options, never selectable as an actual
    transfer source."""
    product_id = (product_id or "").strip()
    doc = db[INVENTORY_CACHE_COLLECTION].find_one(
        {"_id": "latest", "items.product_id": product_id}, {"items.$": 1},
    )
    items = (doc or {}).get("items") or []
    item = items[0] if items else {}
    if item.get("product_id") != product_id:
        return {"product_id": product_id, "description": None, "unit_of_measure": None, "locations": []}

    locations = []
    for loc in item.get("locations", []):
        qty = loc.get("qty") or 0
        if qty <= 0:
            continue
        usable = is_usable_stock_status(loc.get("stock_status"), loc.get("restricted", False))
        if not usable and not include_non_usable:
            continue
        warehouse_id = _warehouse_id_from_logistics_area_id(loc.get("logistics_area_id"))
        if not warehouse_id:
            continue
        locations.append({
            "site_id": _site_id_from_full_site(loc.get("site")),
            "warehouse_id": warehouse_id,
            "warehouse_name": loc.get("logistics_area"),
            "qty": qty,
            "is_usable": usable,
            "stock_status": loc.get("stock_status") or None,
        })
    locations.sort(key=lambda l: (0 if l["is_usable"] else 1, -l["qty"]))
    return {
        "product_id": product_id,
        "description": item.get("description"),
        "unit_of_measure": item.get("uom"),
        "locations": locations,
    }



def list_known_warehouses_for_site(db, site_id: str) -> list:
    """Every real warehouse/logistics-area SAP has ever reported stock in
    for this site, across ALL products (not just one) - backs the "Ship-to
    Location" dropdown. Deliberately NOT hardcoded to the spec's own
    illustrative 5-type list (RM/SFG/FG/SCRAP/CONSUME) - live-checked (Aug
    2026) this tenant's real warehouse types vary a lot per site (P3 alone
    has QC/RTV/SEG/zone-level RM, P1 has JW/PRD/SCR/RTV, P8 has none of
    those) - a fixed guess would both hide real options and offer ones
    that don't exist at that particular site."""
    site_id = (site_id or "").strip().upper()
    doc = db[INVENTORY_CACHE_COLLECTION].find_one({"_id": "latest"}, {"items.locations": 1})
    seen = {}
    for item in (doc or {}).get("items", []):
        for loc in item.get("locations", []):
            if _site_id_from_full_site(loc.get("site")) != site_id:
                continue
            warehouse_id = _warehouse_id_from_logistics_area_id(loc.get("logistics_area_id"))
            if warehouse_id:
                seen[warehouse_id] = loc.get("logistics_area")
    return sorted(({"warehouse_id": k, "warehouse_name": v} for k, v in seen.items()), key=lambda w: w["warehouse_id"])


def ship_to_sites_for_ship_from_site(db, ship_from_site_id: str) -> list:
    """Same-Company Sites only, excluding the Ship-from Site itself - backs
    the "Ship-to Site" dropdown."""
    ship_from_company, _ = company_and_set_of_books_for_site(ship_from_site_id)
    sites = list_known_sites(db)
    return [
        s for s in sites
        if s.strip().upper() != (ship_from_site_id or "").strip().upper()
        and company_and_set_of_books_for_site(s)[0] == ship_from_company
    ]


def suggest_source_warehouse(db, product_id: str, ship_to_site_id: str = None) -> dict:
    """"AI suggests, only suggest" (Aug 2026, user's explicit ask) - a
    transparent, explainable ranking rather than an LLM call: an LLM adds
    latency/cost without making a purely-numeric "which site has the most
    surplus stock" ranking any smarter or more reliable, and a
    deterministic pick is one every user can verify by eye against the
    same numbers shown on screen. Always excludes the Ship-to Site itself
    (never suggest transferring a site's own stock to itself) and never
    auto-applies - the caller/frontend must still let the user accept or
    ignore it."""
    stock = get_product_stock_locations(db, product_id)
    ship_to_site_id = (ship_to_site_id or "").strip().upper()
    candidates = [l for l in stock["locations"] if l["site_id"] != ship_to_site_id]
    if not candidates:
        reason = "No usable stock found for this item at any other site." if stock["locations"] else "No usable stock found for this item anywhere."
        return {"suggested": None, "reason": reason}
    best = candidates[0]  # get_product_stock_locations already sorts by qty desc
    qty_display = f"{best['qty']:g}"
    return {
        "suggested": best,
        "reason": f"Highest available stock: {qty_display} {stock.get('unit_of_measure') or ''} at {best['site_id']} / {best['warehouse_name'] or best['warehouse_id']}".strip(),
    }


class StockTransferValidationError(Exception):
    pass


class StockTransferOrderNotFoundError(Exception):
    pass


def _next_sto_id(db) -> str:
    """STO-000001, STO-000002, ... - simple incrementing counter, same
    spirit as this app's other locally-generated IDs (e.g. store_requests'
    issue_id)."""
    counter = db["counters"].find_one_and_update(
        {"_id": "stock_transfer_order"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER,
    )
    return f"STO-{counter['seq']:06d}"


def create_stock_transfer_order(db, payload: dict, created_by: str) -> dict:
    """Full server-side re-validation (defense in depth - the frontend
    already enforces every one of these rules) against the CURRENT cache,
    since stock/site data can move between when the user opened the screen
    and when they click Create. Persists as status "pending_sap" - see
    module docstring; there is no live SAP write yet.

    A single STO may contain items from DIFFERENT warehouses, but they must
    all resolve to the SAME Ship-from Site - the real SAP CustomerRequirement
    object has exactly one ShipFromSiteID per header (see module docstring),
    so a genuinely cross-site multi-item request must become two STOs."""
    items = payload.get("items") or []
    if not items:
        raise StockTransferValidationError("At least one item is required.")

    ship_to_site_id = (payload.get("ship_to_site_id") or "").strip().upper()
    ship_to_location_id = (payload.get("ship_to_location_id") or "").strip()
    requested_delivery_date = (payload.get("requested_delivery_date") or "").strip()
    if not ship_to_site_id:
        raise StockTransferValidationError("Ship-to Site is required.")
    if not ship_to_location_id:
        raise StockTransferValidationError("Ship-to Location is required.")
    if not requested_delivery_date:
        raise StockTransferValidationError("Requested Delivery Date is required.")
    try:
        delivery_date = date.fromisoformat(requested_delivery_date)
    except ValueError:
        raise StockTransferValidationError("Requested Delivery Date is not a valid date.")
    if delivery_date < datetime.now(timezone.utc).date():
        raise StockTransferValidationError("Requested Delivery Date cannot be earlier than today.")

    # GST / E-way bill compliance fields (Aug 2026, user's explicit ask) -
    # mandatory, but NOT yet pushed to SAP - see module docstring (pending
    # Basis exposing a write path for these custom fields on the Stock
    # Transfer Delivery document). Captured + enforced regardless.
    transportation_mode = (payload.get("transportation_mode") or "").strip()
    vehicle_no = (payload.get("vehicle_no") or "").strip()
    place_of_supply = (payload.get("place_of_supply") or "").strip()
    gr_no = (payload.get("gr_no") or "").strip()
    date_of_supply = (payload.get("date_of_supply") or "").strip()
    if not transportation_mode:
        raise StockTransferValidationError("Transportation Mode is required.")
    if not vehicle_no:
        raise StockTransferValidationError("Vehicle No. is required.")
    if not place_of_supply:
        raise StockTransferValidationError("Place Of Supply is required.")
    if not gr_no:
        raise StockTransferValidationError("G.R No. is required.")
    if not date_of_supply:
        raise StockTransferValidationError("Date Of Supply is required.")
    try:
        date.fromisoformat(date_of_supply)
    except ValueError:
        raise StockTransferValidationError("Date Of Supply is not a valid date.")

    ship_to_warehouses = {w["warehouse_id"]: w["warehouse_name"] for w in list_known_warehouses_for_site(db, ship_to_site_id)}
    if ship_to_location_id not in ship_to_warehouses:
        raise StockTransferValidationError(f"'{ship_to_location_id}' is not a known warehouse at Ship-to Site {ship_to_site_id}.")

    resolved_items = []
    ship_from_site_id = None
    for idx, raw in enumerate(items, start=1):
        product_id = (raw.get("product_id") or "").strip()
        source_warehouse_id = (raw.get("source_warehouse_id") or "").strip()
        requested_qty = raw.get("requested_qty")
        if not product_id:
            raise StockTransferValidationError(f"Line {idx}: Product is required.")
        if not source_warehouse_id:
            raise StockTransferValidationError(f"Line {idx} ({product_id}): Source Warehouse is required.")
        if requested_qty is None or requested_qty <= 0:
            raise StockTransferValidationError(f"Line {idx} ({product_id}): Requested Quantity must be greater than zero.")

        stock = get_product_stock_locations(db, product_id)
        location = next((l for l in stock["locations"] if l["warehouse_id"] == source_warehouse_id), None)
        if location is None:
            raise StockTransferValidationError(f"Line {idx} ({product_id}): No usable stock currently found in warehouse '{source_warehouse_id}'.")
        if requested_qty > location["qty"]:
            raise StockTransferValidationError(
                f"Line {idx} ({product_id}): Insufficient Stock. Please enter a quantity equal to or less than the available inventory ({location['qty']:g})."
            )

        item_ship_from_site = location["site_id"]
        if ship_from_site_id is None:
            ship_from_site_id = item_ship_from_site
        elif item_ship_from_site != ship_from_site_id:
            raise StockTransferValidationError(
                f"Line {idx} ({product_id}): its Source Warehouse is at site {item_ship_from_site}, but earlier line(s) ship from {ship_from_site_id}. "
                "A Stock Transfer Order can only have one Ship-from Site - create a separate STO for this item."
            )

        resolved_items.append({
            "line_no": idx,
            "product_id": product_id,
            "description": stock.get("description"),
            "unit_of_measure": stock.get("unit_of_measure"),
            "source_warehouse_id": source_warehouse_id,
            "source_warehouse_name": location.get("warehouse_name"),
            "ship_from_site_id": item_ship_from_site,
            "available_qty": location["qty"],
            "requested_qty": requested_qty,
            "availability_status": "Available",
        })

    if ship_from_site_id == ship_to_site_id:
        raise StockTransferValidationError("Ship-to Site cannot be the same as Ship-from Site.")
    ship_from_company, _ = company_and_set_of_books_for_site(ship_from_site_id)
    ship_to_company, _ = company_and_set_of_books_for_site(ship_to_site_id)
    if ship_from_company != ship_to_company:
        raise StockTransferValidationError(f"Ship-to Site {ship_to_site_id} does not belong to the same Company as Ship-from Site {ship_from_site_id}.")

    now = datetime.now(timezone.utc)
    sto_doc = {
        "_id": _next_sto_id(db),
        "status": "pending_sap",
        # Populated by submit_order_to_sap() if the live SAP write rejects
        # this order (e.g. a site/warehouse master-data mismatch on SAP's
        # side, caught by the Check call or the real Maintain write).
        # Shown verbatim, human-readable, in the order's detail modal -
        # never a raw stack trace/exception repr.
        "error_message": None,
        "created_by": created_by,
        "created_at": now,
        "ship_from_site_id": ship_from_site_id,
        "ship_to_site_id": ship_to_site_id,
        "ship_to_location_id": ship_to_location_id,
        "ship_to_location_name": ship_to_warehouses.get(ship_to_location_id),
        "delivery_priority": "Immediate",
        "requested_delivery_date": requested_delivery_date,
        "transportation_mode": transportation_mode,
        "vehicle_no": vehicle_no,
        "place_of_supply": place_of_supply,
        "gr_no": gr_no,
        "date_of_supply": date_of_supply,
        "items": resolved_items,
    }
    db[STO_COLLECTION].insert_one(sto_doc)
    return sto_doc


def _build_gst_note_text(doc: dict) -> str:
    """One SAP-side, human-readable Note (see sap_sto_client.py's
    GST_NOTE_TYPE_CODE docstring) carrying all 5 GST/e-way-bill fields -
    the real, working alternative to writing the Outbound Delivery's own
    custom fields (confirmed live impossible via any API, Aug 2026)."""
    parts = []
    for label, key in [("Mode", "transportation_mode"), ("Vehicle No", "vehicle_no"),
                        ("Place of Supply", "place_of_supply"), ("GR No", "gr_no"), ("Date of Supply", "date_of_supply")]:
        value = doc.get(key)
        if value:
            parts.append(f"{label}: {value}")
    return "GST/Transport Info - " + "; ".join(parts) if parts else ""


NOTIFICATIONS_COLLECTION = "admin_notifications"
_MISSING_PLANNING_RE = re.compile(r"No valid planning data exists for product (\S+) in site (\S+)")


def _create_missing_planning_notification_if_matched(db, sto_id: str, error_message: str) -> None:
    """Admin notification (Aug 2026, user's explicit ask): SAP's own STO
    check failure "No valid planning data exists for product X in site Y"
    means that product was never set up (Planning/Availability
    Confirmation/Logistics/Valuation) at the destination site - a one-time
    SAP master-data gap an admin can fix from this app's "Action Needed"
    panel (see activate_material_site() in server.py). Upserts on
    (product_id, site_id) so a repeatedly-failing order doesn't spam
    duplicate notifications."""
    m = _MISSING_PLANNING_RE.search(error_message)
    if not m:
        return
    product_id, site_id = m.group(1), m.group(2)
    db[NOTIFICATIONS_COLLECTION].update_one(
        {"type": "missing_planning_data", "product_id": product_id, "site_id": site_id, "resolved": False},
        {"$set": {"message": error_message, "sto_id": sto_id},
         "$setOnInsert": {"_id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def list_open_admin_notifications(db) -> list:
    return list(db[NOTIFICATIONS_COLLECTION].find({"resolved": False}, {"_id": 1, "type": 1, "product_id": 1, "site_id": 1, "message": 1, "sto_id": 1, "created_at": 1}).sort("created_at", -1))


def resolve_admin_notification(db, notification_id: str) -> None:
    db[NOTIFICATIONS_COLLECTION].update_one({"_id": notification_id}, {"$set": {"resolved": True, "resolved_at": datetime.now(timezone.utc)}})


def submit_order_to_sap(db, sap_sto_client, sto_id: str, job_id: str = None) -> dict:
    """Runs SAP's Check operation first (always-on safety net, not a
    togglable dry-run - user's explicit ask, Aug 2026), then - only if that
    comes back clean - the real Maintain write. Mutates the STO's Mongo doc
    in place with the outcome (status + error_message + the real SAP
    ID/UUID on success), same shape the detail modal already reads.

    If `job_id` is given, updates the job doc's `step` field at each stage
    ("checking" -> "creating") so the frontend's progress-bar dialog can
    show real, not simulated, progress. Goods Issue / Outbound Delivery are
    NOT part of this pipeline yet - see module docstring "Full Goods Issue
    automation" note; once Basis exposes those 2 services this function is
    the place to add "posting_goods_issue"/"confirming_warehouse_task"
    steps, same pattern."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferValidationError(f"Stock Transfer Order {sto_id} not found.")

    requested_local_datetime = f"{doc['requested_delivery_date']}T12:00:00.0000000Z"
    items = [
        {
            "product_id": it["product_id"],
            "requested_qty": it["requested_qty"],
            "unit_code": it.get("unit_of_measure") or "EA",
            "description": it.get("description"),
            "requested_local_datetime": requested_local_datetime,
        }
        for it in doc["items"]
    ]

    note_text = _build_gst_note_text(doc)
    try:
        if job_id:
            job_store.update_job(db, job_id, {"step": "checking"})
        # SAP's own ShipToLocationID field (confirmed live, Aug 2026) is a
        # distinct "Location" master-data ID, NOT a warehouse/logistics-area
        # code - SAP's own docs' examples always set it equal to the Site
        # ID, and a real warehouse code (e.g. "P2-RM", this app's own
        # `ship_to_location_id` used for local display/tracking) was
        # rejected live with "does not match account ... (Site)". Always
        # send the Ship-to SITE ID here, regardless of which specific
        # warehouse the user picked in this app's own UI.
        sap_sto_client.check(doc["ship_from_site_id"], doc["ship_to_site_id"], doc["ship_to_site_id"], items, note_text)
        if job_id:
            job_store.update_job(db, job_id, {"step": "creating"})
        result = sap_sto_client.maintain(doc["ship_from_site_id"], doc["ship_to_site_id"], doc["ship_to_site_id"], items, note_text)
    except SAPSTOError as e:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"status": "sap_failed", "error_message": str(e)}})
        _create_missing_planning_notification_if_matched(db, sto_id, str(e))
        raise

    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "status": "created_in_sap",
        "error_message": None,
        "sap_order_id": result["id"],
        "sap_order_uuid": result["uuid"],
        "gi_status": "awaiting_delivery",
        "gst_note_pushed": bool(note_text),
    }})
    return {"sap_order_id": result["id"], "sap_order_uuid": result["uuid"]}


def _live_source_stock_qty(sap_inventory_client, ship_from_site_id: str, source_warehouse_id: str, product_id: str) -> float:
    """Sums CURRENT, usable stock for one product at one exact warehouse
    (Aug 27 2026, user's explicit ask - a Goods Issue retry/auto-recheck
    should look at real, fresh SAP stock, not a stale local number)."""
    full_warehouse_id = f"{ship_from_site_id}/{source_warehouse_id}"
    rows = sap_inventory_client.get_inventory_detail(warehouse_ids=[full_warehouse_id])
    return sum(
        row.get("qty") or 0 for row in rows
        if row.get("product_id") == product_id and is_usable_stock_status(row.get("stock_status"), row.get("restricted", False))
    )


def try_post_goods_issue(db, sap_outbound_delivery_client, sap_inventory_client, sto_id: str) -> str:
    """One poll attempt: looks for the Outbound Delivery Request Item SAP
    has produced from this STO's Customer Requirement (see
    sap_outbound_delivery_client.py docstring for the safe UUID-based
    match) and, if found and still open, live-checks the source
    warehouse's REAL current stock before attempting the Goods Issue
    (Aug 27 2026 addition - user's explicit ask, following a real GI
    failure "Inventory in logistics area not available": posting blind
    and letting SAP reject it wastes a SAP-side attempt and gives a raw
    error; checking first lets us give a clear "waiting on stock" status
    and keep auto-retrying every poll tick without ever hitting SAP's own
    error log for something that's genuinely just "not here yet").
    Returns "waiting" (SAP hasn't produced the delivery yet, OR stock is
    still insufficient - caller should poll again later), "posted"
    (done), or raises SAPOutboundDeliveryError on a real SAP-side
    rejection of the Goods Issue itself. Mutates the STO doc's
    `gi_status` fields.

    GST fields are recorded on the Customer Requirement's own Note at
    creation time instead (see `_build_gst_note_text`/
    submit_order_to_sap) - confirmed live the Outbound Delivery
    Request/Delivery reject ANY field write via API the instant SAP's own
    scheduler picks them up, before this job ever gets a chance."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferValidationError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") == "posted":
        return "posted"

    delivery_item = sap_outbound_delivery_client.find_delivery_request_item(doc["sap_order_uuid"])
    if not delivery_item:
        return "waiting"

    # OrderFulfilmentProcessingStatusCode: 1=Not Started, 2=In Process,
    # 3=Finished - only post Goods Issue on one that isn't already done
    # (e.g. a retry after a transient error on our own earlier attempt).
    if delivery_item.get("order_fulfilment_status") == "3":
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": "posted", "gi_error": None, "gi_job_running": False,
            "outbound_delivery_object_id": delivery_item["object_id"],
        }})
        return "posted"

    # Single-item STOs only (this app's own current scope for GI
    # automation) - use the first (only) resolved line's source warehouse.
    item = (doc.get("items") or [{}])[0]
    available_qty = _live_source_stock_qty(sap_inventory_client, doc["ship_from_site_id"], item.get("source_warehouse_id"), item.get("product_id"))
    if available_qty < (item.get("requested_qty") or 0):
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": "insufficient_stock",
            "gi_error": f"Insufficient live stock in {item.get('source_warehouse_id')} for {item.get('product_id')} - "
                        f"needed {item.get('requested_qty'):g}, currently available {available_qty:g}. "
                        "Automatically re-checking every 20s.",
            "outbound_delivery_object_id": delivery_item["object_id"],
        }})
        return "waiting"

    try:
        sap_outbound_delivery_client.post_goods_issue(delivery_item["object_id"])
    except SAPOutboundDeliveryError as e:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": "failed", "gi_error": str(e), "gi_job_running": False,
            "outbound_delivery_object_id": delivery_item["object_id"],
        }})
        raise

    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "gi_status": "posted", "gi_error": None, "gi_job_running": False,
        "outbound_delivery_object_id": delivery_item["object_id"],
    }})
    return "posted"


def mark_gi_job_started(db, sto_id: str) -> None:
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_job_running": True}})


def mark_goods_issue_timed_out(db, sto_id: str, note: str, stock_note: str = None) -> None:
    """`stock_note` (Aug 27 2026) - use a stock-specific message when the
    20-min window expires while gi_status was "insufficient_stock" (the
    delivery WAS found, stock was just short) instead of the generic
    "SAP hasn't produced the Outbound Delivery Request" message, which
    would be factually wrong in that case."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}, {"gi_status": 1})
    final_note = stock_note if (doc or {}).get("gi_status") == "insufficient_stock" and stock_note else note
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_status": "not_found_timeout", "gi_error": final_note, "gi_job_running": False}})


def reset_goods_issue_for_retry(db, sto_id: str) -> dict:
    """Manual "Retry Goods Issue" (Aug 27 2026, user's explicit ask) - for
    an order stuck on "failed" (a real SAP rejection) or
    "not_found_timeout" (the 20-min auto-poll gave up). Flips the status
    back to "awaiting_delivery" immediately (UI feedback) and hands off
    to the SAME try_post_goods_issue polling loop, which now always
    live-checks stock first - see module docstring above."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("status") != "created_in_sap":
        raise StockTransferValidationError("Goods Issue can only be retried for an order already created in SAP.")
    if doc.get("gi_status") not in ("failed", "not_found_timeout"):
        raise StockTransferValidationError("Goods Issue is not currently in a failed/timed-out state for this order.")
    if doc.get("gi_job_running"):
        raise StockTransferValidationError("A Goods Issue check is already running for this order - please wait for it to finish.")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_status": "awaiting_delivery", "gi_error": None}})
    return doc


def list_stock_transfer_orders(db, limit: int = 100) -> list:
    return list(db[STO_COLLECTION].find({}).sort("created_at", -1).limit(limit))


def get_stock_transfer_order(db, sto_id: str) -> dict:
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    return doc


# "Let's build to test only" AI feature (Aug 2026, user's explicit ask) -
# GPT-5.4-mini primary (user's pick - fast/cheap, good enough for this
# simple text-to-form extraction), Claude Haiku 4.5 fallback (user's pick)
# so one provider's outage doesn't kill the feature.
_NL_PARSE_MODELS = [("openai", "gpt-5.4-mini"), ("anthropic", "claude-haiku-4-5-20251001")]


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


async def parse_natural_language_transfer_request(text: str, known_sites: list) -> dict:
    """Free-text box like "transfer 500 of ITEM-001 to P2" parsed into form
    fields for the user to REVIEW - this NEVER creates or submits an STO by
    itself, the frontend always requires an explicit Create click after."""
    import os
    from emergentintegrations.llm.chat import LlmChat, UserMessage

    api_key = os.environ["EMERGENT_LLM_KEY"]
    today = datetime.now(timezone.utc).date().isoformat()
    system_message = (
        "Extract a Stock Transfer Order request from the user's free text. "
        "Return ONLY a JSON object with exactly these keys: "
        '"product_id" (string or null), "quantity" (number or null), '
        '"ship_to_site_id" (string or null - must be one of: ' + ", ".join(known_sites) + '), '
        f'"requested_delivery_date" (string "YYYY-MM-DD" or null - resolve relative dates like "today"/"tomorrow" using today = {today}). '
        "If a value isn't mentioned, use null - never invent a value. No prose, no markdown fences, just the raw JSON object."
    )
    last_error = None
    for provider, model in _NL_PARSE_MODELS:
        try:
            chat = LlmChat(
                api_key=api_key, session_id=f"stock-transfer-nl-{uuid.uuid4().hex[:8]}", system_message=system_message,
            ).with_model(provider, model)
            response_text = await chat.send_message(UserMessage(text=text))
            parsed = _extract_json(response_text)
            return {
                "product_id": parsed.get("product_id"),
                "quantity": parsed.get("quantity"),
                "ship_to_site_id": parsed.get("ship_to_site_id"),
                "requested_delivery_date": parsed.get("requested_delivery_date"),
                "model_used": f"{provider}/{model}",
            }
        except Exception as e:
            last_error = e
            logger.warning(f"Stock Transfer NL parse: {provider}/{model} failed, trying next option: {e}")
            continue
    raise StockTransferValidationError(f"Could not understand that request right now ({last_error}) - please fill the form manually.")
