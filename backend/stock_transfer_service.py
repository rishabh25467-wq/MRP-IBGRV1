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
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone, date

from pymongo import ReturnDocument

from sap_wip_clearing_client import company_and_set_of_books_for_site
from production_confirmation_service import is_usable_stock_status
from inventory_service import list_known_sites
from sap_sto_client import SAPSTOError
from sap_outbound_delivery_client import SAPOutboundDeliveryError
import hsn_cache_service
import company_cache_service
import job_store
import sap_playwright_outbound_gi_service

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


def get_product_stock_locations(db, product_id: str, include_non_usable: bool = False, sap_hsn_client=None) -> dict:
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
    product_id = (product_id or "").strip().upper()
    hsn_code = hsn_cache_service.get_hsn_codes_cached(db, [product_id], sap_hsn_client).get(product_id)
    doc = db[INVENTORY_CACHE_COLLECTION].find_one(
        {"_id": "latest", "items.product_id": product_id}, {"items.$": 1},
    )
    items = (doc or {}).get("items") or []
    item = items[0] if items else {}
    if item.get("product_id") != product_id:
        return {"product_id": product_id, "description": None, "unit_of_measure": None, "hsn_code": hsn_code, "locations": []}

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
        "hsn_code": hsn_code,
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


def create_stock_transfer_order(db, payload: dict, created_by: str, sap_hsn_client=None) -> dict:
    """Full server-side re-validation (defense in depth - the frontend
    already enforces every one of these rules) against the CURRENT cache,
    since stock/site data can move between when the user opened the screen
    and when they click Create. Persists as status "pending_sap" - see
    module docstring; there is no live SAP write yet.

    A single STO may contain items from DIFFERENT warehouses, but they must
    all resolve to the SAME Ship-from Site - the real SAP CustomerRequirement
    object has exactly one ShipFromSiteID per header (see module docstring),
    so a genuinely cross-site multi-item request must become two STOs.

    Each resolved line also carries its live SAP `hsn_code` (Aug 27 2026,
    user's explicit ask - shown on the form as it populates, same source
    used for the ERP portal sync) - a single batched lookup for every item
    on this order, never invented if SAP has no HSN code for that material."""
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
    # Freight Forwarder / Transporter name (Aug 27 2026, user's explicit
    # ask - mandatory) - written to the SAP GST Note (_build_gst_note_text)
    # AND the legacy ERP portal's own `Trans` field (sync_to_erp_portal),
    # plus printed on the Delivery Note's Transport box and the Gate
    # Pass's "Transport No" field.
    freight_forwarder = (payload.get("freight_forwarder") or "").strip()
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
    if not freight_forwarder:
        raise StockTransferValidationError("Freight Forwarder is required.")
    try:
        date.fromisoformat(date_of_supply)
    except ValueError:
        raise StockTransferValidationError("Date Of Supply is not a valid date.")

    ship_to_warehouses = {w["warehouse_id"]: w["warehouse_name"] for w in list_known_warehouses_for_site(db, ship_to_site_id)}
    if ship_to_location_id not in ship_to_warehouses:
        raise StockTransferValidationError(f"'{ship_to_location_id}' is not a known warehouse at Ship-to Site {ship_to_site_id}.")

    resolved_items = []
    ship_from_site_id = None
    hsn_codes = hsn_cache_service.get_hsn_codes_cached(db, [(raw.get("product_id") or "").strip().upper() for raw in items], sap_hsn_client)
    for idx, raw in enumerate(items, start=1):
        product_id = (raw.get("product_id") or "").strip().upper()
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
            "hsn_code": hsn_codes.get(product_id),
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
        "freight_forwarder": freight_forwarder,
        "items": resolved_items,
    }
    db[STO_COLLECTION].insert_one(sto_doc)
    return sto_doc


def _build_gst_note_text(doc: dict, item_price_hsn: list = None) -> str:
    """One SAP-side, human-readable Note (see sap_sto_client.py's
    GST_NOTE_TYPE_CODE docstring) carrying all 5 GST/e-way-bill fields -
    the real, working alternative to writing the Outbound Delivery's own
    custom fields (confirmed live impossible via any API, Aug 2026).

    Also carries each item's live SAP HSN Code + Rate (Aug 27 2026,
    user's explicit ask - "price needs to go with hsn on the note",
    since neither reaches the actual Delivery Challan screen either -
    same SAP-side lockout - this Note is the only place in SAP itself
    where a human can see them, right on the Customer Requirement)."""
    parts = []
    for label, key in [("Mode", "transportation_mode"), ("Vehicle No", "vehicle_no"),
                        ("Place of Supply", "place_of_supply"), ("GR No", "gr_no"), ("Date of Supply", "date_of_supply"),
                        ("Freight Forwarder", "freight_forwarder")]:
        value = doc.get(key)
        if value:
            parts.append(f"{label}: {value}")
    text = "GST/Transport Info - " + "; ".join(parts) if parts else ""
    if item_price_hsn:
        item_lines = []
        for entry in item_price_hsn:
            bits = [entry["product_id"]]
            if entry.get("hsn_code"):
                bits.append(f"HSN {entry['hsn_code']}")
            if entry.get("rate") is not None:
                bits.append(f"Rate {entry['rate']:.2f}")
            item_lines.append(" ".join(bits))
        if item_lines:
            text += (" | " if text else "") + "Items: " + "; ".join(item_lines)
    return text[:1000]


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


def _price_hsn_for_note(db, doc: dict, sap_valuation_client) -> list:
    """Live SAP Moving Average price per item, paired with the `hsn_code`
    already stored on each item at creation time (Aug 27 2026, for
    `_build_gst_note_text`'s "Items:" section)."""
    if not sap_valuation_client:
        return []
    product_ids = [it["product_id"] for it in doc["items"]]
    product_uuid_by_id = {
        c["_id"]: c.get("product_uuid")
        for c in db["component_master"].find({"_id": {"$in": product_ids}}, {"product_uuid": 1})
    }
    product_uuids = [u for u in product_uuid_by_id.values() if u]
    costs = sap_valuation_client.get_standard_costs(product_uuids) if product_uuids else {}
    entries = []
    for it in doc["items"]:
        product_uuid = product_uuid_by_id.get(it["product_id"])
        cost = costs.get(product_uuid.upper()) if product_uuid else None
        entries.append({
            "product_id": it["product_id"],
            "hsn_code": it.get("hsn_code"),
            "rate": cost["amount"] if cost else None,
        })
    return entries


def submit_order_to_sap(db, sap_sto_client, sto_id: str, job_id: str = None, sap_valuation_client=None) -> dict:
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

    note_text = _build_gst_note_text(doc, _price_hsn_for_note(db, doc, sap_valuation_client))
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


# Aug 27 2026, "one combined delivery" attempt #5 (see
# sap_outbound_delivery_client.py module docstring) - how many extra
# 20s poll ticks (GOODS_ISSUE_POLL_INTERVAL_SECONDS in server.py) to
# keep retrying the Delivery lookup+release for once every line's GI has
# posted, before giving up and marking "posted" anyway with whatever
# Delivery ID(s) were found so far (same permissive, never-block fallback
# this already had for the plain ID lookup).
MAX_RELEASE_POLL_ATTEMPTS = 5


def _release_ready_deliveries(db, sap_outbound_delivery_client, sto_id: str, doc: dict, item_uuids: list, existing_delivery_ids: list) -> tuple:
    """Looks up whatever real Outbound Delivery object(s) SAP has
    produced so far from `item_uuids` and explicitly releases any not
    already released (tracked via the doc's own
    `released_delivery_object_ids`, so a Delivery released on an earlier
    poll tick is never released twice). Returns (covered, delivery_ids) -
    `covered` is True only once EVERY given uuid has resolved to some
    Delivery (SAP's own OData link can take longer to become queryable
    than the GI post itself - confirmed live, order 30411, still empty
    immediately after posting, present ~30s later - an empty/partial
    result here just means "not indexed yet", not a failure)."""
    item_uuids = [u for u in item_uuids if u]
    if not item_uuids:
        return True, existing_delivery_ids
    try:
        objects = sap_outbound_delivery_client.find_outbound_delivery_objects(item_uuids)
    except Exception as e:
        logger.warning(f"Stock Transfer Order {sto_id}: could not look up Outbound Delivery object(s) yet: {e}")
        return False, existing_delivery_ids

    already_released = set(doc.get("released_delivery_object_ids") or [])
    delivery_ids = list(existing_delivery_ids)
    newly_released = []
    for obj in objects:
        object_id = obj.get("object_id")
        if obj.get("id") and obj["id"] not in delivery_ids:
            delivery_ids.append(obj["id"])
        if not object_id or object_id in already_released or object_id in newly_released:
            continue
        try:
            sap_outbound_delivery_client.release_outbound_delivery(object_id)
        except SAPOutboundDeliveryError as e:
            # Aug 28 2026: a delivery combined+released via the Playwright
            # UI flow (sap_playwright_outbound_gi_service.py) will always
            # fail this API release call ("already released") - the ID
            # is still recorded above regardless, so this is cosmetic.
            logger.warning(f"Stock Transfer Order {sto_id}: could not release Outbound Delivery {obj.get('id') or object_id} (may already be released elsewhere): {e}")
            continue
        newly_released.append(object_id)

    if newly_released:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$addToSet": {"released_delivery_object_ids": {"$each": newly_released}}})

    found_uuids = {o.get("item_uuid") for o in objects if o.get("item_uuid")}
    covered = set(item_uuids).issubset(found_uuids)
    return covered, delivery_ids


def _build_line_status(doc: dict, delivery_items: list, insufficient_products: dict = None, failed_products: dict = None, force_shipped: set = None) -> list:
    """Per-line shipping status (Aug 2026, user's explicit ask) - backs
    ONLY the STO detail view's expanded items table; the list view's
    own header badge deliberately stays a single combined status per
    the user's own call ("one combined header is better on the transfer
    screen, click to see detail"). SAP's own item-level
    OrderFulfilmentProcessingStatusCode ("3"=Finished) is the only real
    per-line signal that exists for the multi-line/Playwright-combined
    path - every line in that path shares one Goods Issue outcome, so
    `force_shipped`/`failed_products` let the caller stamp every line in
    that shared batch with the one outcome it actually got."""
    insufficient_products = insufficient_products or {}
    failed_products = failed_products or {}
    force_shipped = force_shipped or set()
    shipped_products = force_shipped | {d["product_id"] for d in delivery_items if d.get("product_id") and d.get("order_fulfilment_status") == "3"}
    result = []
    for it in (doc.get("items") or []):
        pid = it["product_id"]
        if pid in failed_products:
            status, note = "failed", failed_products[pid]
        elif pid in insufficient_products:
            status, note = "insufficient_stock", insufficient_products[pid]
        elif pid in shipped_products:
            status, note = "shipped", None
        else:
            status, note = "pending", None
        result.append({"line_no": it.get("line_no"), "product_id": pid, "status": status, "note": note})
    return result


def _try_post_goods_issue_multiline(db, sap_outbound_delivery_client, sap_inventory_client, sto_id: str, doc: dict, pending: list, lines_by_product: dict, object_ids: list, existing_delivery_ids: list, delivery_items: list) -> str:
    """Multi-line path (Aug 28 2026) - see try_post_goods_issue's own
    docstring for why this can't use the per-line API loop. Same
    per-line live-stock pre-check as the single-line path (own copy,
    since it pops from `lines_by_product` too), then hands the whole
    order to sap_playwright_outbound_gi_service in ONE call covering
    every line at once."""
    insufficient_notes = []
    insufficient_products = {}
    for d in pending:
        candidates = lines_by_product.get(d.get("product_id")) or []
        line = candidates.pop(0) if candidates else None
        if not line:
            continue
        available_qty = _live_source_stock_qty(sap_inventory_client, doc["ship_from_site_id"], line.get("source_warehouse_id"), d.get("product_id"))
        if available_qty < (line.get("requested_qty") or 0):
            note = f"needed {line.get('requested_qty'):g} in {line.get('source_warehouse_id')}, currently available {available_qty:g}"
            insufficient_notes.append(f"{d.get('product_id')} - {note}")
            insufficient_products[d.get("product_id")] = note
    if insufficient_notes:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": "insufficient_stock",
            "gi_error": "Automatically re-checking every 20s. Still waiting on live stock for: " + "; ".join(insufficient_notes),
            "gi_job_running": False,
            "outbound_delivery_object_id": object_ids[0] if object_ids else None,
            "outbound_delivery_object_ids": object_ids,
            "outbound_delivery_ids": existing_delivery_ids,
            "gi_line_status": _build_line_status(doc, delivery_items, insufficient_products=insufficient_products),
        }})
        return "waiting"

    sap_order_id = (doc.get("sap_order_id") or "").lstrip("0") or doc.get("sap_order_id")
    all_uuids = [d.get("uuid") for d in pending if d.get("uuid")]
    metadata = {
        "vehicle_no": doc.get("vehicle_no"),
        "transportation_mode": doc.get("transportation_mode"),
        "place_of_supply": doc.get("place_of_supply"),
        "gr_no": doc.get("gr_no"),
        "date_of_supply": doc.get("date_of_supply"),
    }
    try:
        ui_result = asyncio.run(sap_playwright_outbound_gi_service.combine_and_post_goods_issue_via_ui(
            os.environ["SAP_USERNAME"], os.environ["SAP_PASSWORD"], sap_order_id, metadata, sap_outbound_delivery_client, all_uuids,
        ))
    except Exception as e:
        # A Playwright/infra hiccup (click timeout, browser crash, nav
        # failure) is NOT a SAP business rejection - raising
        # SAPOutboundDeliveryError here would stop the 20-min poll loop
        # for good (see server.py's _run_goods_issue_job), exactly the
        # same trap the outer poll loop's own generic `except Exception`
        # already avoids. Log and let the next poll tick retry with a
        # fresh browser session instead.
        logger.warning(f"Stock Transfer Order {sto_id}: Playwright combine+GI attempt hit a transient error, will retry: {e}")
        return "waiting"

    if ui_result["status"] == "waiting":
        return "waiting"
    if ui_result["status"] == "failed":
        # All-or-nothing for this shared batch - a single Playwright
        # combine+Release call covers every `pending` line at once, so
        # every one of them shares this same failure (see
        # _build_line_status's own docstring).
        failed_products = {d.get("product_id"): ui_result["error"] for d in pending}
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": "failed", "gi_error": ui_result["error"], "gi_job_running": False,
            "outbound_delivery_object_id": object_ids[0] if object_ids else None,
            "outbound_delivery_object_ids": object_ids,
            "outbound_delivery_ids": existing_delivery_ids,
            "gi_line_status": _build_line_status(doc, delivery_items, failed_products=failed_products),
        }})
        raise SAPOutboundDeliveryError(ui_result["error"])

    # ui_result["status"] == "posted" - Playwright's own "Release" click
    # already released the combined delivery AND posted its Goods Issue
    # (confirmed live) - delivery_ids came straight from the SAP OData
    # lookup it already did, no separate release call needed here.
    delivery_ids = ui_result["delivery_ids"]
    if len(delivery_ids) != 1:
        logger.warning(f"Stock Transfer Order {sto_id}: expected 1 combined delivery, SAP shows {len(delivery_ids)}: {delivery_ids} - needs manual SAP review")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "gi_status": "posted",
        "gi_posted_at": datetime.now(timezone.utc),
        "gi_error": None if len(delivery_ids) == 1 else f"Combined into {len(delivery_ids)} deliveries instead of 1 - please verify in SAP",
        "gi_job_running": False,
        "outbound_delivery_object_id": object_ids[0] if object_ids else None,
        "outbound_delivery_object_ids": object_ids,
        "outbound_delivery_ids": delivery_ids,
        "gi_line_status": _build_line_status(doc, delivery_items, force_shipped={d.get("product_id") for d in pending}),
    }})
    return "posted"


def try_post_goods_issue(db, sap_outbound_delivery_client, sap_inventory_client, sto_id: str) -> str:
    """One poll attempt: looks for EVERY Outbound Delivery Request Item
    SAP has produced from this STO's Customer Requirement - one per line
    item, see sap_outbound_delivery_client.py docstring for the safe
    UUID-based match - and, for each one still open, live-checks its OWN
    source warehouse's REAL current stock before attempting the Goods
    Issue (Aug 27 2026 addition - user's explicit ask, following a real
    GI failure "Inventory in logistics area not available": posting
    blind and letting SAP reject it wastes a SAP-side attempt and gives
    a raw error; checking first lets us give a clear "waiting on stock"
    status and keep auto-retrying every poll tick without ever hitting
    SAP's own error log for something that's genuinely just "not here
    yet").

    Aug 27 2026 bug fix (real incident: STO-000011 / SAP order 30280):
    this used to only ever look at delivery_items[0] and doc["items"][0]
    - correct for a single-line STO, but for a multi-line one it posted
    Goods Issue for just the FIRST line's Outbound Delivery Request Item
    and never even attempted the rest, so SAP created a real Outbound
    Delivery containing only that one line while the others stayed stuck
    at the source site forever, with the app reporting "Goods Issue
    posted" as if the whole order had shipped.

    Aug 27 2026, "one delivery per order" investigation (real incident:
    STO-000046/order 30336 - user reported 3 separate deliveries for 3
    lines instead of one; 2 further live attempts followed, all at THIS
    (Goods Issue) layer - grouping by `parent_object_id`
    (PGIInBackground rejected it: "object does not exist", confirmed via
    $metadata it's item-scoped only), then `OutboundDeliveryRequestAllocate`
    (header-scoped, but rejected: "Project outbound delivery request
    reference missing or not valid" - likely built for a different SAP
    scenario entirely). THE REAL FIX WAS AT A DIFFERENT LAYER: the SAP
    admin (this tenant's own user) showed that a Stock Transfer Order's
    own "Delivery Rule" field (Multiple Deliveries vs Complete Delivery)
    - set at ORDER CREATE time, not at Goods Issue time - is what
    actually decides this. Fixed in sap_sto_client.py by setting
    `CompleteDeliveryRequestedIndicator=true` there instead - no change
    needed here at all; kept posting once per line below since that's
    still simply how PGIInBackground works, but SAP itself will now
    combine every line's resulting delivery for a NEW order into one,
    since the header-level rule tells it to hold everything together
    from creation onward.

    Aug 27 2026, attempt #5 (real incident: SAP order 30411, 2 lines -
    the STO-creation fix above DID combine them into one shared Outbound
    Delivery Request document, confirmed live, but they still ended up
    as 2 separate Deliveries - PGIInBackground is genuinely item-scoped,
    confirmed via $metadata, no batch parameter exists on it at all).
    Every line below now posts its Goods Issue with `auto_release=False`
    (see sap_outbound_delivery_client.py) instead of relying on that
    call's own inline auto-release, and `_release_ready_deliveries`
    explicitly releases whatever Delivery object(s) result only once
    it's checked what every line in this order actually resolved to -
    UNVERIFIED whether this actually gets SAP to combine them, but safe
    either way since every Delivery still always gets explicitly
    released, nothing is left stuck "open".

    Returns "waiting" (SAP hasn't produced the delivery yet, OR at least
    one line's stock is still insufficient - caller should poll again
    later), "posted" (every line done), or raises
    SAPOutboundDeliveryError on a real SAP-side rejection of the Goods
    Issue itself. Mutates the STO doc's `gi_status` fields.

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

    delivery_items = sap_outbound_delivery_client.find_delivery_request_items(doc["sap_order_uuid"])
    if not delivery_items:
        return "waiting"

    object_ids = [d["object_id"] for d in delivery_items if d.get("object_id")]
    # Position-aware matching (testing_agent iteration_127 hardening):
    # a plain product_id->line dict collapses two STO lines that share
    # the SAME product_id but different source_warehouse_id - pop each
    # match off its own product's queue instead, so a duplicate-product
    # multi-line STO checks/posts each line against its OWN warehouse.
    lines_by_product = {}
    for it in (doc.get("items") or []):
        lines_by_product.setdefault(it["product_id"], []).append(it)
    # OrderFulfilmentProcessingStatusCode: 1=Not Started, 2=In Process,
    # 3=Finished - only (re-)attempt lines that aren't already done (a
    # retry after a transient error, or a line finished on an earlier
    # poll tick while a sibling line was still short on stock).
    pending = [d for d in delivery_items if d.get("order_fulfilment_status") != "3"]
    existing_delivery_ids = doc.get("outbound_delivery_ids") or []

    if not pending:
        release_attempts = (doc.get("gi_release_attempts") or 0) + 1
        all_uuids = [d.get("uuid") for d in delivery_items]
        covered, delivery_ids = _release_ready_deliveries(db, sap_outbound_delivery_client, sto_id, doc, all_uuids, existing_delivery_ids)
        if not covered and release_attempts < MAX_RELEASE_POLL_ATTEMPTS:
            # Aug 27 2026 (attempt #5): every line is Finished, but SAP
            # hasn't yet made every Delivery object queryable via OData -
            # keep polling (bounded, see MAX_RELEASE_POLL_ATTEMPTS) rather
            # than marking "posted" (which would stop the poll loop in
            # server.py's _run_goods_issue_job for good) before we've had
            # a real chance to release everything.
            db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_release_attempts": release_attempts, "outbound_delivery_ids": delivery_ids}})
            return "waiting"
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": "posted", "gi_posted_at": datetime.now(timezone.utc), "gi_error": None, "gi_job_running": False,
            "outbound_delivery_object_id": object_ids[0] if object_ids else None,
            "outbound_delivery_object_ids": object_ids,
            "outbound_delivery_ids": delivery_ids,
            "gi_line_status": _build_line_status(doc, delivery_items),
        }})
        return "posted"

    # Aug 27 2026, attempts #6/#7 tried and CONFIRMED DEAD ENDS live (see
    # sap_outbound_delivery_client.py module docstring): #6
    # SLRequestDeliveryExecution needs a pre-existing "Site Logistics
    # Request" this app has no service to create; #7 SLPGIInBackground
    # (schedule-line-scoped GI) posts fine but still produces one
    # Delivery per line, identical to the loop below. #8 (Aug 28 2026):
    # SAP's own documented PartialDeliveryControlCode "3" (paired with
    # CompleteDeliveryRequestedIndicator=true) also CONFIRMED DEAD live
    # (order 30433) - identical 2-deliveries-for-2-lines result, this
    # tenant's Ship-to Party Account Master Data silently overrides it.
    # THE REAL FIX (Aug 28 2026): none of these API layers can combine a
    # multi-line order's Deliveries - only SAP's own UI can (see
    # sap_playwright_outbound_gi_service.py module docstring for the live
    # proof). Multi-line orders below go through that instead of the
    # per-line loop; a single-line order has nothing to combine, so it
    # keeps using the fast, already-working API path unchanged.
    if len(doc.get("items") or []) > 1:
        return _try_post_goods_issue_multiline(db, sap_outbound_delivery_client, sap_inventory_client, sto_id, doc, pending, lines_by_product, object_ids, existing_delivery_ids, delivery_items)

    insufficient_notes = []
    insufficient_products = {}
    failed_notes = []
    failed_products = {}
    newly_posted_item_uuids = []
    for d in pending:
        candidates = lines_by_product.get(d.get("product_id")) or []
        line = candidates.pop(0) if candidates else None
        if not line:
            logger.warning(
                f"Stock Transfer Order {sto_id}: Outbound Delivery Request line for product "
                f"{d.get('product_id')!r} has no matching STO line locally - posting Goods Issue "
                "without a local live-stock pre-check (SAP's own validation is the only safeguard here)."
            )
        if line:
            available_qty = _live_source_stock_qty(sap_inventory_client, doc["ship_from_site_id"], line.get("source_warehouse_id"), d.get("product_id"))
            if available_qty < (line.get("requested_qty") or 0):
                note = f"needed {line.get('requested_qty'):g} in {line.get('source_warehouse_id')}, currently available {available_qty:g}"
                insufficient_notes.append(f"{d.get('product_id')} - {note}")
                insufficient_products[d.get("product_id")] = note
                continue
        try:
            sap_outbound_delivery_client.post_goods_issue(d["object_id"], auto_release=False)
            newly_posted_item_uuids.append(d.get("uuid"))
        except SAPOutboundDeliveryError as e:
            # Aug 27 2026 hardening (testing_agent iteration_127): don't
            # abort the whole loop on the first SAP rejection - that
            # would leave every remaining not-yet-attempted line unposted
            # and stuck, the exact same "partial shipment reported as one
            # success/failure" shape as the original STO-000011 bug, just
            # triggered by a SAP-side rejection instead of the old
            # first-row-only lookup. Keep trying every other line.
            failed_notes.append(f"{d.get('product_id')}: {e}")
            failed_products[d.get("product_id")] = str(e)
            continue

    delivery_ids = existing_delivery_ids
    covered = True
    if newly_posted_item_uuids:
        # Aug 27 2026 (attempt #5): release is now separate from the GI
        # post itself (see _release_ready_deliveries / module docstring)
        # - a lookup/release hiccup here is still cosmetic-only for the
        # gi_status itself (the GI already succeeded for these lines),
        # it only delays how soon this returns "posted" (see `covered`
        # below), never fails the whole job.
        covered, delivery_ids = _release_ready_deliveries(db, sap_outbound_delivery_client, sto_id, doc, newly_posted_item_uuids, existing_delivery_ids)

    if failed_notes or insufficient_notes:
        gi_status = "failed" if failed_notes else "insufficient_stock"
        parts = []
        if failed_notes:
            parts.append("SAP rejected: " + "; ".join(failed_notes))
        if insufficient_notes:
            parts.append("Automatically re-checking every 20s. Still waiting on live stock for: " + "; ".join(insufficient_notes))
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
            "gi_status": gi_status, "gi_error": " | ".join(parts), "gi_job_running": False,
            "outbound_delivery_object_id": object_ids[0] if object_ids else None,
            "outbound_delivery_object_ids": object_ids,
            "outbound_delivery_ids": delivery_ids,
            "gi_line_status": _build_line_status(doc, delivery_items, insufficient_products=insufficient_products, failed_products=failed_products, force_shipped={d.get("product_id") for d in pending if d.get("uuid") in newly_posted_item_uuids}),
        }})
        if failed_notes:
            raise SAPOutboundDeliveryError(" | ".join(parts))
        return "waiting"

    if not covered:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_release_attempts": 1, "outbound_delivery_ids": delivery_ids}})
        return "waiting"

    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "gi_status": "posted", "gi_posted_at": datetime.now(timezone.utc), "gi_error": None, "gi_job_running": False,
        "outbound_delivery_object_id": object_ids[0] if object_ids else None,
        "outbound_delivery_object_ids": object_ids,
        "outbound_delivery_ids": delivery_ids,
        "gi_line_status": _build_line_status(doc, delivery_items, force_shipped={d.get("product_id") for d in pending}),
    }})
    return "posted"


def mark_gi_job_started(db, sto_id: str) -> None:
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_job_running": True}})


def find_orphaned_gi_jobs(db) -> list:
    """Call once at process startup, before any new Goods Issue job can
    start (mirrors job_store.recover_orphaned_jobs, same root cause).
    `_run_goods_issue_job` (server.py) is a plain in-memory asyncio task,
    not tracked in the recoverable `background_jobs` collection - if the
    backend restarts/redeploys while it's still polling, that task is
    gone for good but `gi_job_running` stays True forever (nothing else
    ever clears it), leaving the order stuck in whatever `gi_status` it
    was last in with NO retry button ever appearing (only "failed"/
    "not_found_timeout" show one) and no code path left to revive it.
    Real incident (Aug 29 2026): STO-000013 stuck on plain
    "awaiting_delivery" indefinitely right after a production deploy.
    Returns the sto_ids so the caller can resume each one's polling loop
    fresh - safe even if it had actually finished right before the
    restart, since try_post_goods_issue() checks gi_status=="posted"
    first and returns immediately."""
    return [d["_id"] for d in db[STO_COLLECTION].find({"gi_job_running": True}, {"_id": 1})]


def heal_stuck_pending_sap_orders(db) -> list:
    """Call once at process startup - a broader, retroactive sweep on top
    of server.py's `_recovered_jobs` loop. That loop only reacts to a
    "submit_sto" job that is STILL in job_store's ORPHANABLE_JOB_STATUSES
    at THIS particular restart; a job that already got marked "failed" by
    an EARLIER restart (e.g. before the "submit_sto" `kind` tag / this
    healing code even existed) is invisible to it forever, so its STO doc
    stays "pending_sap" showing the plain "Submitting to SAP now" banner
    indefinitely with no Retry button - real incident, STO-000014, Aug 29
    2026: survived one restart+redeploy untouched because the job behind
    it had already failed on an even earlier restart. This instead looks
    directly at every "pending_sap" STO and only leaves ones with a
    GENUINELY currently-`running` submit_sto job alone (a real in-flight
    submission, not something to touch) - everything else (no job at all,
    or a job that's failed/done/anything else) gets flipped to
    "sap_failed" with a clear message so the existing Retry button
    appears, regardless of which restart originally orphaned it."""
    import job_store
    stuck = list(db[STO_COLLECTION].find({"status": "pending_sap"}, {"_id": 1}))
    healed = []
    for doc in stuck:
        sto_id = doc["_id"]
        running_job = db[job_store.COLLECTION_NAME].find_one(
            {"sto_id": sto_id, "kind": "submit_sto", "status": "running"}
        )
        if running_job:
            continue
        db[STO_COLLECTION].update_one(
            {"_id": sto_id, "status": "pending_sap"},
            {"$set": {
                "status": "sap_failed",
                "error_message": "This order never finished submitting to SAP (likely interrupted by an earlier backend restart) - please retry.",
            }},
        )
        healed.append(sto_id)
    return healed


def ensure_gi_job_stopped(db, sto_id: str, error: str) -> None:
    """Defensive safety net (Aug 29 2026, found while testing the orphan-
    resume fix above) for server.py's _run_goods_issue_job: guarantees
    gi_job_running actually gets cleared on a terminal SAPOutboundDeliveryError,
    even for a rejection raised before try_post_goods_issue's own
    per-line code got a chance to update the STO doc itself. Idempotent
    no-op if gi_status is already a terminal state (posted/failed/
    not_found_timeout/insufficient_stock)."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}, {"gi_status": 1})
    if (doc or {}).get("gi_status") in ("posted", "failed", "not_found_timeout", "insufficient_stock"):
        return
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_status": "failed", "gi_error": error, "gi_job_running": False}})


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
            raw_site = (parsed.get("ship_to_site_id") or "").strip().upper()
            # Case-insensitive match against known_sites (user's explicit
            # bug report - "p3" instead of "P3" silently failed to load,
            # since the LLM just echoes back whatever case the user typed
            # and the dropdown options are always uppercase).
            matched_site = next((s for s in known_sites if s.upper() == raw_site), raw_site or None)
            return {
                "product_id": parsed.get("product_id"),
                "quantity": parsed.get("quantity"),
                "ship_to_site_id": matched_site,
                "requested_delivery_date": parsed.get("requested_delivery_date"),
                "model_used": f"{provider}/{model}",
            }
        except Exception as e:
            last_error = e
            logger.warning(f"Stock Transfer NL parse: {provider}/{model} failed, trying next option: {e}")
            continue
    raise StockTransferValidationError(f"Could not understand that request right now ({last_error}) - please fill the form manually.")


def sync_to_erp_portal(db, erp_portal_client, sap_valuation_client, sto_id: str) -> None:
    """Writes this Stock Transfer Order into the legacy Radish ERP portal
    (Aug 27 2026, user's explicit ask) via its own stored procedures - see
    erp_portal_client.py. Fires right after the order is created in SAP
    (same moment as the GST note), independent of Goods Issue - a
    completely separate, best-effort sync (same pattern as
    gst_note_pushed), never blocks or fails the SAP write itself.

    Field mapping (user's explicit instructions, Aug 27 2026):
      CompCode = Ship-from Site ID directly (e.g. "P3") - NOT the
        Site->Company mapping used elsewhere for WIP Clearing (RI/RT never
        needs to reach this legacy portal at all, user's explicit fix).
      Pcode = the ship-to site's own ERP-internal plant code, looked up
        from the ERP's own `comp.pcode` column (via company_cache_service
        - Aug 27 2026 fix, confirmed live against real DeliveryChallan
        history: e.g. Ship-to P8 -> Pcode "R2970", never the raw site ID
        "P8" itself). Falls back to the raw site ID only if that site has
        no pcode registered in `comp` at all (site P6 today - user's
        explicit choice for this gap).
      Rate/Amt/Amount/TaxableAmt = SAP's live Moving Average price x
        quantity (never Standard Cost - see sap_valuation_client.py). This
        is the ONE place this price is ever fetched live - also persisted
        onto this order's own `items` here (Aug 27 2026, user's explicit
        ask: "should be stored in mongo per record since u write to erp
        anyway"), so get_delivery_note_data below never needs to re-fetch
        it live on every single print.
      HSN_no = reuses the `hsn_code` ALREADY resolved and stored on each
        item at create_stock_transfer_order time (hsn_cache_service) -
        never a fresh SAP call here.
      Trans = Freight Forwarder / Transporter name (Aug 27 2026, user's
        explicit ask - now a mandatory field on the STO form, see
        create_stock_transfer_order). Emp_no/ElecRefNo/Padd_Code1/
        Padd_Code2/Term1-3 all still left blank - user's explicit
        instruction (not captured/needed today).
      InvStk_status = always "Open" on creation (user's explicit ask,
        Aug 27 2026) - see erp_portal_client.create_delivery_challan
        (the stored proc itself has no parameter for this column at all,
        so it's set via one extra UPDATE right after the insert).
    """
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")

    product_ids = [item["product_id"] for item in doc["items"]]
    product_uuid_by_id = {
        c["_id"]: c.get("product_uuid")
        for c in db["component_master"].find({"_id": {"$in": product_ids}}, {"product_uuid": 1})
    }
    product_uuids = [u for u in product_uuid_by_id.values() if u]
    costs = sap_valuation_client.get_standard_costs(product_uuids) if product_uuids else {}

    sale_date = datetime.strptime(doc["date_of_supply"], "%Y-%m-%d")
    line_items = []
    stored_items = []
    total_amount = 0.0
    for item in doc["items"]:
        product_uuid = product_uuid_by_id.get(item["product_id"])
        cost = costs.get(product_uuid.upper()) if product_uuid else None
        rate = cost["amount"] if cost else 0.0
        qty = item["requested_qty"]
        amt = round(rate * qty, 3)
        total_amount += amt
        line_items.append({
            "product_id": item["product_id"], "description": item.get("description"),
            "hsn_no": item.get("hsn_code"), "qty": qty, "unit": item.get("unit_of_measure") or "EA",
            "rate": rate, "amt": amt, "dis_amt": 0, "taxable_amt": amt, "remark": None,
        })
        stored_items.append({**item, "rate": rate, "amount": amt})

    ship_to_company = company_cache_service.get_cached_company_info(db, [doc["ship_to_site_id"]], erp_portal_client).get(doc["ship_to_site_id"])
    pcode = (ship_to_company or {}).get("pcode") or doc["ship_to_site_id"]

    header = {
        "comp_code": doc["ship_from_site_id"],
        "elec_ref_no": None,
        "sale_date": sale_date,
        "pcode": pcode,
        "padd_code1": None, "padd_code2": None,
        "trans": doc.get("freight_forwarder"),
        "veh_no": doc.get("vehicle_no"),
        "gr_no": doc.get("gr_no"),
        "gr_date": doc.get("date_of_supply"),
        "marks": doc.get("place_of_supply"),
        "amount": round(total_amount, 2),
        "tdis_amt": 0,
        "ttaxable_amt": round(total_amount, 2),
        "term1": None, "term2": None, "term3": None,
        "emp_no": None,
    }
    result = erp_portal_client.create_delivery_challan(header, line_items)
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "erp_portal_status": "synced", "erp_portal_error": None,
        "erp_sale_no": result["sale_no"], "erp_sale_noc": result["sale_noc"],
        "items": stored_items,
    }})


def mark_erp_portal_failed(db, sto_id: str, error: str) -> None:
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"erp_portal_status": "failed", "erp_portal_error": error}})


def reset_erp_portal_sync_for_retry(db, sto_id: str) -> None:
    """Manual "Retry ERP Sync" (Aug 27 2026, user's explicit ask - "no
    legal document can be created" while this is stuck failed). Only
    allowed while genuinely "failed" - guards against ever calling
    sync_to_erp_portal twice for the same order, which would create a
    DUPLICATE Delivery Challan (new Sale_No) in the legacy portal for
    the same physical stock movement."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("erp_portal_status") != "failed":
        raise StockTransferValidationError("ERP Portal sync is not currently in a failed state for this order.")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"erp_portal_status": "retrying", "erp_portal_error": None}})


def get_delivery_note_data(db, erp_portal_client, sto_id: str) -> dict:
    """Data for the in-app "Delivery Challan" print view (Aug 27 2026,
    user's explicit ask, referencing SAP's own printed template as the
    layout target), plus the ERP portal's own Sale_No/Sale_Noc as the
    document's Serial Number (user's explicit ask - "display both").

    Aug 27 2026 (later this session, performance fix - user's explicit
    ask: "should be stored in mongo per record since u write to erp
    anyway"): rate/amount/hsn_code are no longer fetched live from SAP on
    every print - they're read straight off this order's own `items`,
    where sync_to_erp_portal already persisted them (Moving Average
    price, HSN Code) the ONE time this order was synced to the ERP
    portal. An order that hasn't synced yet simply has no rate/hsn_code
    stored (shows 0 / "—") - printing before syncing was never a
    supported flow anyway (the Serial Number itself only exists post-sync).

    Company Name/Address/GSTIN/PAN/current-fiscal-`session` for BOTH the
    Ship-from and Ship-to sites comes from company_cache_service (Mongo
    cache of the ERP's own `comp` table, refreshed periodically - not a
    live MS SQL query per print, same user's-ask as above) - this is what
    lets the print view show the correct "Radish Technologies" vs "Ray
    International" letterhead. If a site is missing from `comp` entirely
    (e.g. a newer site not yet onboarded there), falls back to just the
    right company NAME via the same Site->Company mapping WIP Clearing
    uses, so the letterhead is never wrong even without a full address.
    Serial Number format is the user's explicit spec:
    "{Ship-from CompCode}-{Sale_Noc}-{session}"."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")

    items = []
    total_amount = 0.0
    for item in doc["items"]:
        rate = item.get("rate") or 0.0
        qty = item["requested_qty"]
        amount = item.get("amount")
        if amount is None:
            amount = round(rate * qty, 3)
        total_amount += amount
        items.append({
            "product_id": item["product_id"],
            "description": item.get("description"),
            "hsn_code": item.get("hsn_code"),
            "qty": qty,
            "unit": item.get("unit_of_measure") or "EA",
            "rate": rate,
            "amount": amount,
        })

    company_info = company_cache_service.get_cached_company_info(
        db, [doc["ship_from_site_id"], doc["ship_to_site_id"]], erp_portal_client,
    )

    def _company_or_fallback(site_id):
        company = company_info.get(site_id)
        if company:
            return company
        company_code, _ = company_and_set_of_books_for_site(site_id)
        return {"company_name": "RAY INTERNATIONAL" if company_code == "RI" else "RADISH TECHNOLOGIES"}

    ship_from_company = _company_or_fallback(doc["ship_from_site_id"])
    ship_to_company = _company_or_fallback(doc["ship_to_site_id"])
    session = ship_from_company.get("session")
    erp_sale_noc = doc.get("erp_sale_noc")
    serial_number = f"{doc['ship_from_site_id']}-{erp_sale_noc}-{session}" if erp_sale_noc and session else (str(erp_sale_noc) if erp_sale_noc else None)

    return {
        "sto_id": sto_id,
        "erp_sale_no": doc.get("erp_sale_no"),
        "erp_sale_noc": erp_sale_noc,
        "serial_number": serial_number,
        "date_of_supply": doc.get("date_of_supply"),
        "ship_from_site_id": doc["ship_from_site_id"],
        "ship_to_site_id": doc["ship_to_site_id"],
        "ship_from_company": ship_from_company,
        "ship_to_company": ship_to_company,
        "vehicle_no": doc.get("vehicle_no"),
        "gr_no": doc.get("gr_no"),
        "transportation_mode": doc.get("transportation_mode"),
        "place_of_supply": doc.get("place_of_supply"),
        "freight_forwarder": doc.get("freight_forwarder"),
        "items": items,
        "total_amount": round(total_amount, 2),
    }
