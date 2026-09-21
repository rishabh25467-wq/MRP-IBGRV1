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

# Sep 3 2026, user's explicit ask (real incident: legacy ERP serial
# number mismatch for Ship-from P1W/P2W): SAP's own Site ID and the
# legacy ERP's `comp.Ccode` are identical for every site EXCEPT these
# two - the ERP's `comp` table has no Ccode "P1W"/"P2W" at all (only
# rows Ccode="W1"/"W2", each with "P1W"/"P2W" as THAT row's own `pcode`
# column - confirmed live). sync_to_erp_portal was sending the raw SAP
# site ID as CompCode, landing new Delivery Challans under an
# unregistered "P1W"/"P2W" CompCode disconnected from the ERP's real
# "W1"/"W2" Sale_No sequence. Only applies going forward - user's
# explicit ask NOT to touch past synced orders (those keep whatever
# CompCode they were actually written with).
_ERP_COMP_CODE_OVERRIDE = {"P1W": "W1", "P2W": "W2"}


def _erp_comp_code_for_ship_from(ship_from_site_id: str) -> str:
    return _ERP_COMP_CODE_OVERRIDE.get(ship_from_site_id, ship_from_site_id)

INVENTORY_CACHE_COLLECTION = "inventory_cache"
# Real incident fix (Sep 2026, Order 30518/Delivery P1D1-492): a single
# Playwright combine+GI attempt with NO outer bound could hang inside
# the browser automation indefinitely (a selector that never resolves,
# a stalled SAP page navigation, etc.) - since _run_goods_issue_job's own
# 20-min ceiling is only ever checked BETWEEN attempts, one hung attempt
# silently blocked that ceiling from ever firing, leaving the order
# stuck on "opening delivery..." forever with no error and no retry.
# Comfortably above the worst-case legitimate run (login+nav+create
# delivery+metadata fill+up to 4 consistency re-checks ~= 3 min).
GI_PLAYWRIGHT_ATTEMPT_TIMEOUT_SECONDS = 300


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
        loc_site_id = _site_id_from_full_site(loc.get("site"))
        # Sep 21 2026, real live incident (STO-000526/527, P1->P8 and
        # P3->P2): {SITE}-HOLD is a purely TRANSIENT staging warehouse our
        # own _relocate_items_to_source_hold_warehouse moves stock into
        # right before an STO reaches SAP, then out of again via Goods
        # Issue - never a genuine standing "home" for stock. inventory_cache
        # only refreshes every couple hours, so it can show a stale
        # snapshot of HOLD mid-flight (another order's stock, about to be
        # issued out). If a NEW STO's source gets set to HOLD from that
        # stale snapshot, submit_order_to_sap's "already in HOLD, skip
        # relocation" check (source_warehouse_id == hold_warehouse_id)
        # wrongly trusts it - by the time SAP tries to actually source it,
        # HOLD is empty ("Determination of source inventory failed" on
        # every line, live-confirmed for P42417/P26724/P-42152/P27784/etc,
        # all genuinely sitting in P1-QC/P1-RM/P1-SFG instead). Never offer
        # {SITE}-HOLD as a selectable/suggested source for a NEW transfer.
        if warehouse_id == _relocation_hold_warehouse_id(loc_site_id):
            continue
        locations.append({
            "site_id": loc_site_id,
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


def _resolve_and_validate_items(db, items: list, ship_to_site_id: str, sap_hsn_client=None) -> tuple:
    """Sep 21 2026, extracted out of `create_stock_transfer_order` so the
    new `validate_stock_transfer_order` pre-flight check (user's explicit
    ask - "before writing anything, confirm stock status...") can reuse
    the EXACT same cache-based resolution/sufficiency rules without also
    writing a Mongo doc. Behavior unchanged - still raises
    `StockTransferValidationError` on the first bad line, same messages
    as before. Returns (resolved_items, ship_from_site_id)."""
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
    return resolved_items, ship_from_site_id


def validate_stock_transfer_order(db, payload: dict, sap_sto_client=None, sap_valuation_client=None, sap_inventory_client=None, sap_hsn_client=None) -> dict:
    """Sep 21 2026, user's explicit ask ("break down the create STO
    process in 2 parts... step 1 before writing anything, confirm stock
    status, activation, valuation SAP") - a dedicated pre-flight check
    with ZERO side effects: no Mongo doc created, no stock physically
    relocated, no SAP Maintain write. Directly targets the 3 classes of
    real live incident hit this same session (STO-000526/527's stale-
    HOLD-cache stock issue, today's earlier Valuation gaps, and the
    long-running "No valid planning data"/missing-Activation class of
    SAP rejection).

    Per user's explicit ask: only issues that would truly make SAP
    reject the order come back as `level: "error"` (these are what the
    frontend gates "Review & Create" on) - there is no softer
    "warning" class yet, kept here for future use if ever needed.

    1. Cache-based structural resolution (`_resolve_and_validate_items`,
       same rules `create_stock_transfer_order` itself enforces).
    2. Live stock check - the user's explicit ask was to check ONLY the
       exact warehouse/items entered, nothing broader - queries SAP
       directly (not the cache, which can go stale - see the HOLD
       staleness bug fixed earlier this session) for exactly that.
    3. Valuation check at the destination site (reuses the existing
       proactive check from earlier today).
    4. Activation/Planning check - SAP's own read-only `check()` call
       (same one `submit_order_to_sap` makes later; a pure dry-run, no
       write) against the resolved items, run here with zero side
       effects instead of only after the order's already been created."""
    issues = []
    ship_to_site_id = (payload.get("ship_to_site_id") or "").strip().upper()
    raw_items = payload.get("items") or []
    if not raw_items:
        return {"ok": False, "issues": [{"product_id": None, "field": "items", "level": "error", "message": "At least one item is required."}]}
    if not ship_to_site_id:
        return {"ok": False, "issues": [{"product_id": None, "field": "ship_to_site_id", "level": "error", "message": "Ship-to Site is required."}]}

    try:
        resolved_items, ship_from_site_id = _resolve_and_validate_items(db, raw_items, ship_to_site_id, sap_hsn_client)
    except StockTransferValidationError as e:
        return {"ok": False, "issues": [{"product_id": None, "field": "items", "level": "error", "message": str(e)}]}

    if sap_inventory_client:
        for it in resolved_items:
            try:
                rows = sap_inventory_client.get_inventory_detail(
                    site_id=it["ship_from_site_id"], warehouse_ids=[it["source_warehouse_id"]], product_ids=[it["product_id"]])
                live_qty = sum(r.get("qty") or 0 for r in rows if is_usable_stock_status(r.get("stock_status"), r.get("restricted", False)))
            except Exception as e:
                logger.warning(f"Validate STO: live stock check failed for {it['product_id']}/{it['source_warehouse_id']} ({e}) - skipping this item's live check")
                continue
            if live_qty < it["requested_qty"]:
                issues.append({
                    "product_id": it["product_id"], "field": "stock", "level": "error",
                    "message": f"{it['product_id']}: SAP shows only {live_qty:g} {it['unit_of_measure'] or ''} available right now in {it['source_warehouse_id']}, but {it['requested_qty']:g} was requested.".strip(),
                })

    fake_doc = {"ship_to_site_id": ship_to_site_id, "items": resolved_items}
    for product_id in _missing_valuation_products(db, sap_valuation_client, fake_doc):
        issues.append({
            "product_id": product_id, "field": "valuation", "level": "error",
            "message": f"{product_id} has no Cost/Valuation set up at site {ship_to_site_id} yet - ask your SAP admin to activate Valuation there, then re-validate.",
        })

    if sap_sto_client:
        check_items = [{
            "product_id": it["product_id"], "requested_qty": it["requested_qty"],
            "unit_code": it.get("unit_of_measure") or "EA", "description": it.get("description"),
            "requested_local_datetime": f"{(payload.get('requested_delivery_date') or datetime.now(timezone.utc).date().isoformat())}T12:00:00.0000000Z",
        } for it in resolved_items]
        try:
            sap_sto_client.check(ship_from_site_id, ship_to_site_id, ship_to_site_id, check_items, None)
        except SAPSTOError as e:
            error_text = str(e)
            m = _MISSING_PLANNING_RE.search(error_text)
            m2 = _MISSING_SUPPLY_PLANNING_RE.search(error_text)
            if m:
                issues.append({"product_id": m.group(1), "field": "activation", "level": "error", "message": f"{m.group(1)} is not activated (Planning/Logistics/Valuation) at site {m.group(2)} yet - ask your SAP admin to activate this material there, then re-validate."})
            elif m2:
                for it in resolved_items:
                    issues.append({"product_id": it["product_id"], "field": "activation", "level": "error", "message": f"Site {m2.group(1)}'s Supply Planning setup is missing - ask your SAP admin to check this site's setup, then re-validate."})
            else:
                issues.append({"product_id": None, "field": "sap", "level": "error", "message": f"SAP rejected this order: {error_text}"})

    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues}


def create_stock_transfer_order(db, payload: dict, created_by: str, sap_hsn_client=None, created_by_user_id: str = None) -> dict:
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
    # Remark (Sep 2 2026, user's explicit ask) - optional free text,
    # positioned right after Freight Forwarder everywhere it's shown.
    remark = (payload.get("remark") or "").strip()
    # Sep 18 2026 architecture shift briefly made these optional (staff
    # were expected to fill them directly in SAP's Delivery screen since
    # Playwright was removed) - REVERTED per user's explicit follow-up
    # ask: these 6 fields are mandatory again here in the app. They are
    # still NEVER pushed to SAP itself (confirmed live, SAP hard-locks
    # these custom fields against any API write once its scheduler picks
    # the order up - see sap_outbound_delivery_client.py's docstring) -
    # only captured/stored on this app's own STO doc, and pushed to the
    # legacy ERP portal for the 4 fields that actually have a column
    # there (Vehicle No./G.R No./Date Of Supply/Freight Forwarder - see
    # sync_to_erp_portal below); Transportation Mode/Place Of Supply have
    # no corresponding ERP proc parameter at all, so they stay app-only.
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
    if not freight_forwarder:
        raise StockTransferValidationError("Freight Forwarder is required.")

    ship_to_warehouses = {w["warehouse_id"]: w["warehouse_name"] for w in list_known_warehouses_for_site(db, ship_to_site_id)}
    if ship_to_location_id not in ship_to_warehouses:
        raise StockTransferValidationError(f"'{ship_to_location_id}' is not a known warehouse at Ship-to Site {ship_to_site_id}.")

    resolved_items, ship_from_site_id = _resolve_and_validate_items(db, items, ship_to_site_id, sap_hsn_client)

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
        # Sep 3 2026, user's explicit ask (regression fix: a "user" role
        # account could see EVERY plant's Stock Transfer Orders on the
        # main page, not just their own) - stable identity to filter on,
        # same id-OR-name pattern already used for Production
        # Confirmation's open-lots (see get_stock_transfer_orders).
        "created_by_user_id": created_by_user_id,
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
        "remark": remark,
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
DAILY_RATE_CACHE_COLLECTION = "daily_valuation_cache"


def _get_daily_cached_costs(db, sap_valuation_client, product_uuids, site_id):
    """Fetches SAP's live Moving Average price at most ONCE PER CALENDAR
    DAY per (site, product) - user's explicit ask (Sep 2026): creating a
    second STO for the same product/site on the same day should reuse
    today's already-fetched rate instead of hitting SAP again. Cached in
    `daily_valuation_cache`, keyed by site+product+date; a fresh SAP call
    only happens for products with no cache entry for today yet. A missing
    price (None - genuinely no valuation found) is never cached, so it's
    retried on the next call rather than silently stuck for the day."""
    product_uuids = list({u.upper() for u in product_uuids if u})
    if not product_uuids:
        return {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached_docs = list(db[DAILY_RATE_CACHE_COLLECTION].find({
        "_id": {"$in": [f"{site_id}:{u}:{today}" for u in product_uuids]}
    }))
    costs = {d["product_uuid"]: {"amount": d["amount"], "currency": d.get("currency")} for d in cached_docs}
    missing = [u for u in product_uuids if u not in costs]
    if missing:
        fresh = sap_valuation_client.get_standard_costs(missing, site_id=site_id)
        for product_uuid, cost in fresh.items():
            costs[product_uuid] = cost
            if cost:
                db[DAILY_RATE_CACHE_COLLECTION].update_one(
                    {"_id": f"{site_id}:{product_uuid}:{today}"},
                    {"$set": {
                        "product_uuid": product_uuid, "site_id": site_id, "date": today,
                        "amount": cost["amount"], "currency": cost.get("currency"),
                        "fetched_at": datetime.now(timezone.utc),
                    }},
                    upsert=True,
                )
    return costs
_MISSING_PLANNING_RE = re.compile(r"No valid planning data exists for product (\S+) in site (\S+)")
# Sep 5 2026, real incident (STO-135, ship-to P8) - SAP returns a
# SECOND, differently-worded message for the exact same underlying
# "product never set up at this site" problem: "Planning /
# Availability / Logistics: Supply planning ID P8; does not exist".
# Unlike the message above, this one never names the product at all
# (just the site) - so every item on the order is treated as a
# candidate and gets its own notification (harmless if a couple turn
# out to already be activated - activate_site() is idempotent).
_MISSING_SUPPLY_PLANNING_RE = re.compile(r"Supply planning ID (\S+); does not exist", re.IGNORECASE)


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
    if m:
        _upsert_missing_planning_notification(db, sto_id, m.group(1), m.group(2), error_message)
        return
    m2 = _MISSING_SUPPLY_PLANNING_RE.search(error_message)
    if not m2:
        return
    site_id = m2.group(1)
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}, {"items": 1})
    for item in (doc or {}).get("items", []):
        _upsert_missing_planning_notification(db, sto_id, item["product_id"], site_id, error_message)


def _upsert_missing_planning_notification(db, sto_id: str, product_id: str, site_id: str, error_message: str, notif_type: str = "missing_planning_data") -> None:
    db[NOTIFICATIONS_COLLECTION].update_one(
        {"type": notif_type, "product_id": product_id, "site_id": site_id, "resolved": False},
        {"$set": {"message": error_message, "sto_id": sto_id},
         "$setOnInsert": {"_id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def _missing_valuation_products(db, sap_valuation_client, doc: dict) -> list:
    """Sep 21 2026, user's explicit ask: proactively check the
    DESTINATION site's Valuation before ever calling SAP's Check step -
    previously a Valuation gap only surfaced much later, at GRN/
    receiving time (see sap_material_valuation_data_client.
    friendly_valuation_error's docstring for the exact same check on
    that side). Returns the list of product_ids on this order with no
    Valuation record at ship_to_site_id yet - empty if all good, OR if
    ship_to_site_id isn't in SAPValuationClient.SITE_TO_PERMANENT_
    ESTABLISHMENT_UUID yet (has_valuation_level's docstring: fail-open,
    never false-block an order over a site we simply don't have a
    mapping for). Also fail-open (log + return []) on any unexpected
    DB/SAP error here - a transient lookup hiccup should never block a
    legitimate order from reaching SAP's own Check step."""
    if not sap_valuation_client:
        return []
    try:
        product_ids = [it["product_id"] for it in doc["items"]]
        uuid_by_id = {
            c["_id"]: c.get("product_uuid")
            for c in db["component_master"].find({"_id": {"$in": product_ids}}, {"product_uuid": 1})
        }
        product_uuids = [u for u in uuid_by_id.values() if u]
        if not product_uuids:
            return []
        has_level = sap_valuation_client.has_valuation_level(product_uuids, doc["ship_to_site_id"])
        if has_level is None:
            return []
        return [
            product_id for product_id in product_ids
            if uuid_by_id.get(product_id) and has_level.get(uuid_by_id[product_id].upper()) is False
        ]
    except Exception as e:
        logger.warning(f"Stock Transfer {doc.get('_id')}: proactive Valuation check failed ({e}) - proceeding without it")
        return []


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
    costs = _get_daily_cached_costs(db, sap_valuation_client, product_uuids, doc.get("ship_from_site_id")) if product_uuids else {}
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


# Sep 18 2026, user's explicit ask + build instruction (real, reproducible
# "Determination of source inventory failed for product X" SAP error at
# Site P8, confirmed on multiple products/warehouses - see PRD.md/
# STO_CONTEXT.md for the full diagnosis). ROOT CAUSE CONFIRMED (user's own
# live screenshot of Site P8's Material Flow "Basic Rule" originally
# showed `Source Logistics Area` hardcoded to `P8-FG`). First attempt
# targeted P8-SFG (disproved live, order 32183). Second attempt targeted
# P8-FG per that rule (live-tested working, STO-000092/order... no error).
# FINAL target per user's explicit correction (same session): P8-HOLD -
# user has set up P8-HOLD as the single staging warehouse for BOTH
# directions at Site P8 (see the mirror-image receiving-side fix,
# _relocate_receipt_from_hold below, which moves P8-HOLD -> target on
# the way IN). Move stock here FIRST, THEN create the STO - matches the
# exact same pattern on the outbound side too, for consistency.
#
# Sep 17 2026, user's explicit ask - generalized from P8-only to every
# site: user confirmed live they've had SAP Basis create the same
# "{SITE}-HOLD" staging warehouse (P1-HOLD, P2-HOLD, P3-HOLD, ...) at
# every site, specifically so this same workaround applies everywhere,
# not just P8. No hardcoded site check anymore - runs for any
# ship_from_site_id as long as sap_goods_movement_client is available.
def _relocation_hold_warehouse_id(site_id: str) -> str:
    return f"{(site_id or '').strip().upper()}-HOLD"


def _relocate_items_to_source_hold_warehouse(db, sto_id: str, sap_goods_movement_client, doc: dict, items: list, sap_inventory_client=None) -> None:
    """Sep 21 2026 fix (real live incident - STO for BOX-P-FMA2/P1):
    previously this ALWAYS moved the FULL `requested_qty` out of
    `source_warehouse_id`, even when {SITE}-HOLD already held some or
    all of that quantity from an earlier relocation/production move -
    so a genuinely satisfiable order (e.g. 78 already in P1-HOLD + 15
    more in P1-SFG, for a 90-unit request) failed with "Not enough
    stock in the source warehouse" because it tried to move all 90 out
    of P1-SFG (which only ever had 15) instead of just the shortfall.
    Now checks {SITE}-HOLD's OWN current balance first via
    `sap_inventory_client` and only moves the shortfall (or skips the
    move entirely if HOLD alone already covers the requested amount)."""
    from store_approval_service import _trigger_goods_movement

    site_id = doc["ship_from_site_id"]
    hold_warehouse_id = _relocation_hold_warehouse_id(site_id)
    owner_party_id, _ = company_and_set_of_books_for_site(site_id)
    relocated_any = False
    for doc_item, item in zip(doc["items"], items):
        source_warehouse_id = doc_item.get("source_warehouse_id")
        if not source_warehouse_id or source_warehouse_id == hold_warehouse_id:
            continue
        move_qty = item["requested_qty"]
        if sap_inventory_client:
            try:
                rows = sap_inventory_client.get_inventory_detail(
                    site_id=site_id, warehouse_ids=[hold_warehouse_id], product_ids=[item["product_id"]])
                hold_qty = sum(r.get("qty") or 0 for r in rows)
            except Exception as e:
                logger.warning(f"STO {sto_id}: could not check {hold_warehouse_id}'s existing balance for {item['product_id']} ({e}) - moving the full requested qty as before")
                hold_qty = 0
            move_qty = max(0, item["requested_qty"] - hold_qty)
            if move_qty <= 0:
                doc_item["p8_relocation"] = {"from": source_warehouse_id, "to": hold_warehouse_id, "skipped_already_in_hold": hold_qty}
                relocated_any = True
                continue
        result = _trigger_goods_movement(
            sap_goods_movement_client, owner_party_id, item["product_id"],
            source_warehouse_id, hold_warehouse_id,
            move_qty, item["unit_code"], site_id,
        )
        if not result.get("ok"):
            raise StockTransferValidationError(
                f"Could not relocate {item['product_id']} from {source_warehouse_id} to "
                f"{hold_warehouse_id} ahead of STO creation: {result.get('error') or result.get('error_detail') or 'unknown SAP error'}"
            )
        doc_item["p8_relocation"] = {
            "from": source_warehouse_id, "to": hold_warehouse_id,
            "gac_id": result.get("external_id"), "moved_qty": move_qty,
        }
        relocated_any = True
    if relocated_any:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"items": doc["items"]}})


def submit_order_to_sap(db, sap_sto_client, sto_id: str, job_id: str = None, sap_valuation_client=None, sap_goods_movement_client=None, sap_inventory_client=None) -> dict:
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

    if sap_goods_movement_client:
        try:
            _relocate_items_to_source_hold_warehouse(db, sto_id, sap_goods_movement_client, doc, items, sap_inventory_client=sap_inventory_client)
        except StockTransferValidationError as e:
            db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"status": "sap_failed", "error_message": str(e)}})
            raise

    note_text = _build_gst_note_text(doc, _price_hsn_for_note(db, doc, sap_valuation_client))
    # Sep 21 2026, user's explicit ask - check the destination site's
    # Valuation BEFORE ever calling SAP's Check step, so a missing
    # Valuation surfaces here (clear message, same Action Needed +
    # Retry flow as a missing-Planning rejection) instead of only much
    # later at GRN/receiving time.
    missing_valuation = _missing_valuation_products(db, sap_valuation_client, doc)
    if missing_valuation:
        products_text = ", ".join(missing_valuation)
        plural = len(missing_valuation) > 1
        message = (
            f"{products_text} {'have' if plural else 'has'} no Cost/Valuation set up at site {doc['ship_to_site_id']} yet - "
            f"ask your SAP admin to activate Valuation for {'these materials' if plural else 'this material'} there, then Retry below."
        )
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"status": "sap_failed", "error_message": message}})
        for product_id in missing_valuation:
            _upsert_missing_planning_notification(db, sto_id, product_id, doc["ship_to_site_id"], message, notif_type="missing_valuation")
        raise StockTransferValidationError(message)
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


def _build_line_status(doc: dict, delivery_items: list, force_shipped: set = None) -> list:
    """Per-line shipping status (Aug 2026, user's explicit ask) - backs
    ONLY the STO detail view's expanded items table. Sep 18 2026, Manual
    Goods Issue shift: only ever "shipped" (analytics confirmed Finished
    + quantities matched, `force_shipped` stamps every line at once) or
    "pending" - no more per-attempt failed/insufficient states, since
    nothing is attempted via API anymore."""
    force_shipped = force_shipped or set()
    shipped_products = force_shipped | {d["product_id"] for d in delivery_items if d.get("product_id") and d.get("order_fulfilment_status") == "3"}
    result = []
    for it in (doc.get("items") or []):
        pid = it["product_id"]
        result.append({"line_no": it.get("line_no"), "product_id": pid, "status": "shipped" if pid in shipped_products else "pending", "note": None})
    return result


MANUAL_GI_QTY_TOLERANCE = 0.01


def _match_analytics_quantities(doc: dict, analytics_rows: list) -> list:
    """Shared by the background auto-poll and "Complete STO Process" -
    CPRODUCT_UUID on the Outbound Delivery Analytics report is, despite
    the name, the plain-text Product ID (same misleadingly-named pattern
    already confirmed live on the equivalent Inbound Delivery report -
    see supplier_shipment_service.check_manual_gr_quantities's
    docstring), so matched directly against doc["items"]["product_id"]
    with no UUID resolution needed. Returns a list of mismatch strings
    (empty if everything matches within MANUAL_GI_QTY_TOLERANCE)."""
    confirmed_by_product = {}
    for r in analytics_rows:
        pid = r.get("product_uuid")
        if pid:
            confirmed_by_product[pid] = confirmed_by_product.get(pid, 0) + (r.get("quantity") or 0)
    requested_by_product = {}
    for it in (doc.get("items") or []):
        requested_by_product[it["product_id"]] = requested_by_product.get(it["product_id"], 0) + (it.get("requested_qty") or 0)
    mismatches = []
    for product_id, requested_qty in requested_by_product.items():
        confirmed_qty = confirmed_by_product.get(product_id, 0.0)
        if abs(requested_qty - confirmed_qty) > MANUAL_GI_QTY_TOLERANCE:
            mismatches.append(f"{product_id}: shipped {requested_qty:g}, SAP shows {confirmed_qty:g}")
    return mismatches


def _finalize_gi_posted(db, sto_id: str, doc: dict, found_ids: list) -> None:
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "gi_status": "posted", "gi_posted_at": datetime.now(timezone.utc), "gi_error": None, "gi_job_running": False,
        "outbound_delivery_ids": found_ids,
        "gi_line_status": _build_line_status(doc, [], force_shipped={it.get("product_id") for it in (doc.get("items") or [])}),
    }})


def try_post_goods_issue(db, sap_outbound_delivery_client, sap_outbound_delivery_analytics_client, sto_id: str) -> str:
    """Sep 18 2026, Manual Goods Issue architecture shift (user's
    explicit ask, confirmed live across 3 independent approaches - the
    plain custom OData service, a custom ABSL Business Object, AND SAP's
    OWN dedicated `ManageODExtensionIn` service, which despite the name
    only supports reading, never writing) that NOTHING can ever write
    SAP's real Delivery custom fields (VehicleNo_KUT/TransportationMode_KUT/
    etc): SAP hard-locks the document for API writes the instant its own
    scheduler picks it up. Only the interactive SAP UI can - so every
    STO, single-line or multi-line, needs staff to manually create the
    Delivery and fill those fields in SAP themselves.

    BUT (real-world proof, Sep 18 2026: STO-000078/Order 32139/Delivery
    P1D1-536 AND STO-000079/Order 32140/Delivery P1D1-537, both posted
    clean with every one of the 6 fields left blank) - Release itself
    does NOT require those fields first, and SAP auto-copies quantities
    from the source order with no manual entry needed either. So the
    ONLY real manual step is the Release click - tried automatically via
    plain API below (`release_outbound_delivery`, works immediately once
    SAP's own "Consistency Status" is ready) before ever pausing for
    actual manual work. No field write of any kind is attempted, ever.

    Returns "posted" (every line Finished in SAP, quantities matched
    within MANUAL_GI_QTY_TOLERANCE - see _match_analytics_quantities) or
    "awaiting_manual_gi" (nothing Finished/matched yet, and/or the
    automatic Release attempt failed - pause, staff complete it
    themselves in SAP, then use "Complete STO Process", see
    check_manual_gi_completion). Never raises."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferValidationError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") == "posted":
        return "posted"
    if not doc.get("gi_delivery_request_id"):
        # Best-effort, purely informational (shown on the "awaiting
        # manual" banner so staff know what to search for in SAP) - a
        # failure here must never block the analytics check below.
        try:
            delivery_items = sap_outbound_delivery_client.find_delivery_request_items(doc["sap_order_uuid"])
            if delivery_items:
                display_id = sap_outbound_delivery_client.get_delivery_request_display_id(delivery_items[0]["parent_object_id"])
                if display_id:
                    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_delivery_request_id": display_id}})
        except Exception as e:
            logger.warning(f"Could not fetch Delivery Request display ID for {sto_id}: {e}")
    try:
        analytics_rows = sap_outbound_delivery_analytics_client.find_deliveries_for_sto(doc.get("sap_order_id") or "")
    except Exception as e:
        logger.warning(f"Stock Transfer Order {sto_id}: Outbound Delivery Analytics check failed, will retry: {e}")
        analytics_rows = []
    found_ids = sorted({r["delivery_id"] for r in analytics_rows if r.get("delivery_id")})
    all_finished = bool(analytics_rows) and all(r.get("finished") for r in analytics_rows)
    if found_ids and all_finished and not _match_analytics_quantities(doc, analytics_rows):
        _finalize_gi_posted(db, sto_id, doc, found_ids)
        return "posted"

    if found_ids and not all_finished:
        try:
            for delivery_id in found_ids:
                object_id = sap_outbound_delivery_client.get_delivery_object_id_by_id(delivery_id)
                sap_outbound_delivery_client.release_outbound_delivery(object_id)
            logger.info(f"Stock Transfer Order {sto_id}: automatic Release of {found_ids} succeeded, re-checking Analytics.")
            try:
                analytics_rows = sap_outbound_delivery_analytics_client.find_deliveries_for_sto(doc.get("sap_order_id") or "")
            except Exception as e:
                logger.warning(f"Stock Transfer Order {sto_id}: post-Release Analytics re-check failed, will confirm on next attempt: {e}")
                analytics_rows = []
            if analytics_rows and all(r.get("finished") for r in analytics_rows) and not _match_analytics_quantities(doc, analytics_rows):
                _finalize_gi_posted(db, sto_id, doc, found_ids)
                return "posted"
        except SAPOutboundDeliveryError as e:
            logger.warning(f"Stock Transfer Order {sto_id}: automatic Release of {found_ids} failed (SAP's Consistency Status likely not ready yet), pausing for manual completion: {e}")

    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "gi_status": "awaiting_manual_gi", "gi_error": None, "gi_job_running": False,
        "outbound_delivery_ids": found_ids,
    }})
    return "awaiting_manual_gi"

MANUAL_GI_QTY_TOLERANCE = 0.01


def check_manual_gi_completion(db, sap_outbound_delivery_analytics_client, sto_id: str) -> dict:
    """"Complete STO Process" button (Sep 18 2026, Manual Goods Issue
    architecture shift, user's explicit ask - mirrors the Manual GRN
    "Re-check SAP" pattern already built for Supplier GRN). Staff click
    this once they've manually created the Delivery, filled its header
    fields (Vehicle No/Transportation Mode/etc.) and posted Goods Issue
    themselves directly in SAP. Re-checks the same Outbound Delivery
    Analytics report (RPSCMOBDB04_Q0001QueryResults) already used by the
    background auto-poll - no Playwright involved either way.

    CPRODUCT_UUID on this report is, despite the name, the plain-text
    Product ID (same misleadingly-named pattern already confirmed live
    on the equivalent Inbound Delivery report - see
    supplier_shipment_service.check_manual_gr_quantities's docstring),
    so quantities are matched directly against doc["items"]["product_id"]
    with no UUID resolution needed.

    Raises StockTransferValidationError (never a hard user-block, unlike
    Manual GRN - a Delivery's quantities are auto-copied from the STO by
    SAP itself, so a real mismatch here should be rare, per user's own
    call) if SAP hasn't produced a Finished Delivery yet, or if the
    quantities genuinely don't match. On a full match, reuses the exact
    same terminal "posted" update the auto/analytics path already uses."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") != "awaiting_manual_gi":
        raise StockTransferValidationError("This order isn't awaiting a manual Goods Issue completion.")
    sap_order_id = (doc.get("sap_order_id") or "").lstrip("0") or doc.get("sap_order_id")
    try:
        analytics_rows = sap_outbound_delivery_analytics_client.find_deliveries_for_sto(sap_order_id)
    except Exception as e:
        raise StockTransferValidationError(f"Could not reach SAP to verify status right now: {e}")
    found_ids = sorted({r["delivery_id"] for r in analytics_rows if r.get("delivery_id")})
    if not found_ids:
        raise StockTransferValidationError(
            "No Delivery found in SAP yet for this order - create the Delivery in SAP and post Goods Issue first, then try again."
        )
    if not all(r.get("finished") for r in analytics_rows):
        raise StockTransferValidationError(
            f"Delivery {', '.join(found_ids)} found in SAP but Goods Issue hasn't been posted yet - post it in SAP first, then try again."
        )
    mismatches = _match_analytics_quantities(doc, analytics_rows)
    if mismatches:
        raise StockTransferValidationError(
            "SAP's confirmed quantity doesn't match this order's requested quantity - " + "; ".join(mismatches) +
            ". Correct it in SAP (or here) and try again."
        )
    _finalize_gi_posted(db, sto_id, doc, found_ids)
    return get_stock_transfer_order(db, sto_id)




def mark_gi_job_started(db, sto_id: str) -> None:
    """Real incident (Aug 29 2026): STO-000015 sat on the plain "waiting
    for SAP to schedule the delivery" banner for 30+ real-world minutes,
    well past the intended 20-min timeout, because that timeout was
    tracked purely via `time.monotonic()` inside `_run_goods_issue_job`
    (server.py) - a counter that resets to zero every time the job
    (re)starts, including every auto-resume after a backend restart. On
    a day with several redeploys, the clock kept getting reset before it
    could ever reach 20 minutes, even though the order had genuinely
    been waiting far longer than that. `gi_job_started_at` is only ever
    set once (first poll attempt, never overwritten on a later resume)
    so the real-world elapsed time survives any number of restarts."""
    db[STO_COLLECTION].update_one(
        {"_id": sto_id, "gi_job_started_at": {"$exists": False}},
        {"$set": {"gi_job_started_at": datetime.now(timezone.utc)}},
    )
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_job_running": True, "gi_progress_phase": "checking_sap"}})


def get_gi_job_started_at(db, sto_id: str) -> datetime:
    """Companion to mark_gi_job_started - always call AFTER it (guarantees
    the field exists by then, first-poll-attempt-wins race excluded since
    both run sequentially in the same caller)."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}, {"gi_job_started_at": 1})
    return doc["gi_job_started_at"]


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


def mark_goods_issue_timed_out(db, sto_id: str, note: str, stock_note: str = None, delivery_request_found_note: str = None) -> None:
    """`stock_note` (Aug 27 2026) - use a stock-specific message when the
    20-min window expires while gi_status was "insufficient_stock" (the
    delivery WAS found, stock was just short) instead of the generic
    "SAP hasn't produced the Outbound Delivery Request" message, which
    would be factually wrong in that case.

    `delivery_request_found_note` (Sep 2026, real incident STO-000074):
    same problem, different real cause - the Outbound Delivery Request
    itself WAS found (its display ID got cached as gi_delivery_request_id
    in try_post_goods_issue, well before this timeout fires), it's the
    LATER "combine into one Delivery Proposal" step
    (_try_post_goods_issue_multiline's Playwright screen) that never saw
    a matching row after 20 minutes. Saying "SAP hasn't produced the
    Outbound Delivery Request" here is factually wrong and misleading -
    the Request demonstrably exists (its own ID is shown to the user
    elsewhere on this same order)."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}, {"gi_status": 1, "gi_delivery_request_id": 1})
    doc = doc or {}
    if doc.get("gi_status") == "insufficient_stock" and stock_note:
        final_note = stock_note
    elif doc.get("gi_delivery_request_id") and delivery_request_found_note:
        final_note = delivery_request_found_note.format(delivery_request_id=doc["gi_delivery_request_id"])
    else:
        final_note = note
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"gi_status": "not_found_timeout", "gi_error": final_note, "gi_job_running": False}})


def is_gi_stop_requested(db, sto_id: str) -> bool:
    """Companion to request_gi_job_stop - see its own docstring."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}, {"gi_stop_requested": 1})
    return bool((doc or {}).get("gi_stop_requested"))


def request_gi_job_stop(db, sto_id: str) -> dict:
    """"Force Stop This Job Now" (user's explicit ask, Sep 2026 - real
    incident, Order 30529/30518, tired of waiting on ANY timer during a
    stuck Goods Issue). Two parts: (1) flips gi_status to "failed" +
    gi_job_running False RIGHT NOW for instant UI feedback (Retry button
    appears immediately) - safe even if a browser attempt is still
    genuinely mid-flight, since nothing here touches SAP itself, only
    this app's own tracking; (2) sets gi_stop_requested so
    _run_goods_issue_job's own loop (server.py), once it eventually
    regains control (bounded by GI_PLAYWRIGHT_ATTEMPT_TIMEOUT_SECONDS - a
    live OS thread running Playwright can't be safely killed mid-action,
    so this can't be made truly instant), exits quietly instead of
    overwriting this stop with whatever that attempt eventually returns."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    if not doc.get("gi_job_running") and doc.get("gi_status") not in ("awaiting_delivery", "insufficient_stock"):
        raise StockTransferValidationError("No Goods Issue automation is currently running for this order.")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "gi_status": "failed",
        "gi_error": "Stopped by user. If a Delivery was already created in SAP for this order, it was NOT deleted or released - check Outbound Deliveries in SAP, or just Retry from here to let this app find and release it automatically.",
        "gi_job_running": False, "gi_stop_requested": True,
    }})
    return doc


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
    db[STO_COLLECTION].update_one(
        {"_id": sto_id},
        {"$set": {"gi_status": "awaiting_delivery", "gi_error": None}, "$unset": {"gi_job_started_at": "", "gi_stop_requested": ""}},
    )
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
        quantity (never Standard Cost - see sap_valuation_client.py),
        fetched at most once per calendar day per site+product (Sep 2026,
        user's explicit ask - see _get_daily_cached_costs) rather than
        live on every single sync. Also persisted onto this order's own
        `items` here (Aug 27 2026, user's explicit ask: "should be stored
        in mongo per record since u write to erp anyway"), so
        get_delivery_note_data below never needs to re-fetch it live on
        every single print.
      HSN_no = reuses the `hsn_code` ALREADY resolved and stored on each
        item at create_stock_transfer_order time (hsn_cache_service) -
        never a fresh SAP call here.
      Trans = Freight Forwarder / Transporter name (Aug 27 2026, user's
        explicit ask - now a mandatory field on the STO form, see
        create_stock_transfer_order). ElecRefNo/Padd_Code1/Padd_Code2/
        Term1-3 all still left blank - user's explicit instruction (not
        captured/needed today).
      Emp_no = this order's own SAP Order ID (Sep 9 2026 fix, real
        incident STO-000202/000203 - see erp_portal_client's own
        duplicate-check docstring note below). Previously always left
        blank per an earlier explicit instruction, but the ERP's own
        Pro_DeliveryChallan_Insert proc rejects a new Challan as a
        duplicate purely on (Plant, Customer, Emp_no, Amount) - it does
        NOT consider date or product at all. Two genuinely different
        STOs shipping the identical product+qty (same Amount) back to
        back is a real, unremarkable scenario (confirmed live: 2
        identical Wall Plate x460 EA shipments 2 minutes apart), and the
        second one was permanently blocked as a "duplicate" of the
        first's own already-successful Challan. Since Order IDs are
        always unique, this alone is enough to stop the false collision
        without the ERP team needing to change their own proc.
      InvStk_status = always "Open" on creation (user's explicit ask,
        Aug 27 2026) - see erp_portal_client.create_delivery_challan
        (the stored proc itself has no parameter for this column at all,
        so it's set via one extra UPDATE right after the insert).
    """
    # Sep 18 2026, user's explicit ask: "do not create any entries on ERP
    # since its polluting the sequence there... hold off" while the P8
    # SAP source-determination issue is unresolved. Env-flag kill switch
    # (lazily read, same reason as store_approval_service.is_dry_run -
    # server.py imports this module before load_dotenv runs) rather than
    # deleting/commenting out the real logic below, so re-enabling later
    # is a single .env flip, not a code change.
    #
    # Bug fix (real user report, Sep 2026 - "why r u refreshing this page
    # again n again?"): this used to just silently `return` here, leaving
    # whatever `mark_erp_portal_syncing`/reset_erp_portal_sync_for_retry
    # had JUST set (a non-terminal "syncing"/"retrying" status) stuck
    # that way forever. The frontend's poll-while-in-flight logic reads
    # exactly that field, so every GI-posted order looked eternally "in
    # progress" and the list kept auto-refreshing every 5s with no way
    # to ever stop. Now stamps a genuinely terminal "paused" status
    # instead, so the frontend can tell "intentionally paused" apart
    # from "actually still working".
    if os.environ.get("STO_ERP_SYNC_PAUSED", "false").lower() == "true":
        logger.info(f"ERP portal sync paused (STO_ERP_SYNC_PAUSED=true) - skipping for {sto_id}.")
        db[STO_COLLECTION].update_one(
            {"_id": sto_id, "erp_portal_status": {"$ne": "synced"}},
            {"$set": {"erp_portal_status": "paused"}},
        )
        return
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")

    product_ids = [item["product_id"] for item in doc["items"]]
    product_uuid_by_id = {
        c["_id"]: c.get("product_uuid")
        for c in db["component_master"].find({"_id": {"$in": product_ids}}, {"product_uuid": 1})
    }
    product_uuids = [u for u in product_uuid_by_id.values() if u]
    costs = _get_daily_cached_costs(db, sap_valuation_client, product_uuids, doc.get("ship_from_site_id")) if product_uuids else {}

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
    erp_comp_code = _erp_comp_code_for_ship_from(doc["ship_from_site_id"])

    header = {
        "comp_code": erp_comp_code,
        "elec_ref_no": None,
        "sale_date": sale_date,
        "pcode": pcode,
        "padd_code1": None, "padd_code2": None,
        "trans": doc.get("freight_forwarder"),
        "veh_no": doc.get("vehicle_no"),
        "gr_no": doc.get("gr_no"),
        "gr_date": doc.get("date_of_supply"),
        # Sep 3 2026, user's explicit ask (was place_of_supply, no
        # documented rationale, real Mark field mismatch found live) -
        # this app's own "Remark" field on the STO form, not
        # Place of Supply.
        "marks": doc.get("remark"),
        "amount": round(total_amount, 2),
        "tdis_amt": 0,
        "ttaxable_amt": round(total_amount, 2),
        "term1": None, "term2": None, "term3": None,
        "emp_no": doc.get("sap_order_id"),
    }
    result = erp_portal_client.create_delivery_challan(header, line_items)
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "erp_portal_status": "synced", "erp_portal_error": None,
        "erp_sale_no": result["sale_no"], "erp_sale_noc": result["sale_noc"],
        # Sep 3 2026: the CompCode actually written to the ERP for this
        # order (see _erp_comp_code_for_ship_from above) - used by
        # get_delivery_note_data's Serial Number instead of recomputing
        # from ship_from_site_id, so pre-fix orders keep printing
        # whatever CompCode they were really synced with.
        "erp_comp_code": erp_comp_code,
        "items": stored_items,
    }, "$unset": {"erp_sync_retry_started_at": ""}})


def mark_erp_portal_failed(db, sto_id: str, error: str) -> None:
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"erp_portal_status": "failed", "erp_portal_error": error}, "$unset": {"erp_sync_retry_started_at": ""}})


def manually_link_erp_sale_no(db, sto_id: str, sale_no: int, sale_noc: int, actor: str) -> None:
    """Admin-only recovery (Sep 9 2026, real incident STO-000202): the
    ERP write can genuinely succeed (record committed, real Sale_No
    assigned) while the app still shows "failed" - e.g. the connection
    dropped right after commit but before the OUTPUT param could be
    read back, or (this incident) 2 back-to-back STOs shipped the exact
    identical product+qty, so ERP's own duplicate-check (Plant+
    Customer+Emp_no+Amount, no date/product) rejected the SECOND one's
    insert as a "duplicate" of the FIRST one's already-successful
    Challan, leaving the first looking "failed" too even though its
    own Sale_No 38682 was real. There is no way to safely auto-detect
    which of 2 identical-shipment orders a pre-existing Challan
    actually belongs to (confirmed live - both orders had the exact
    same Plant/Customer/Product/Qty/Amount), so this requires a human
    to confirm it (e.g. from the physical Challan/vehicle number) - see
    the corresponding /manual-erp-link endpoint, admin/super_admin
    only. Blocked once already "synced" for the same reason
    reset_erp_portal_sync_for_retry is."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("erp_portal_status") == "synced":
        raise StockTransferValidationError("This order has already synced to the ERP Portal.")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "erp_portal_status": "synced", "erp_portal_error": None,
        "erp_sale_no": sale_no, "erp_sale_noc": sale_noc,
        "erp_manually_linked_by": actor, "erp_manually_linked_at": datetime.now(timezone.utc),
    }, "$unset": {"erp_sync_retry_started_at": ""}})


def mark_erp_portal_syncing(db, sto_id: str) -> None:
    """Tracks when the very FIRST (non-retry) ERP sync attempt started
    too, not just retries - see reset_erp_portal_sync_for_retry's own
    docstring for the real incident (STO-000196) this matters for.
    Called right before sync_to_erp_portal on EVERY attempt."""
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "erp_portal_status": "syncing", "erp_sync_retry_started_at": datetime.now(timezone.utc),
    }})


def reset_erp_portal_sync_for_retry(db, sto_id: str) -> None:
    """Manual "Retry ERP Sync" (Aug 27 2026, user's explicit ask - "no
    legal document can be created" while this is stuck failed).
    Blocked ONLY while already "synced" - guards against ever calling
    sync_to_erp_portal twice for an order that already has a real
    Delivery Challan in the legacy portal (would create a DUPLICATE
    Sale_No for the same physical stock movement). Every other state
    (failed/syncing/retrying/never-started) is retriable on demand.

    Sep 9 2026 fix (real incident, STO-000196): this used to also
    require status=="failed" specifically (or a time-based "has it
    been stuck long enough" guess for "retrying") - an orphaned FIRST
    sync attempt (app server restarted/redeployed mid-flight, before
    ever reaching a terminal status) left `erp_portal_status` stuck at
    "syncing" (or unset) forever with no way to retry at all, and a
    server restart is common enough that guessing a "stuck" threshold
    isn't worth the complexity - per user's explicit ask, just let the
    button always work instead of making the user wait out a timer."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("erp_portal_status") == "synced":
        raise StockTransferValidationError("This order has already synced to the ERP Portal - retry is not needed.")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "erp_portal_status": "retrying", "erp_portal_error": None,
        "erp_sync_retry_started_at": datetime.now(timezone.utc),
    }})


def get_delivery_note_data(db, erp_portal_client, sto_id: str, sap_valuation_client=None, sap_outbound_delivery_client=None) -> dict:
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
    portal.

    Sep 2026 fix (real incident STO-000415, price missing/blank on the
    printed challan): a stored rate of 0/None means that ONE-TIME
    sync_to_erp_portal price snapshot either never ran yet, or its SAP
    valuation lookup happened to come back empty that day (e.g. a
    transient SAP timeout) - previously that stayed frozen at 0 forever,
    since nothing ever revisited it afterwards. Any item still missing a
    real stored rate now gets ONE live SAP valuation retry right here
    (same `_get_daily_cached_costs` helper sync_to_erp_portal/
    submit_order_to_sap already use, so a genuinely-still-missing SAP
    price isn't re-hit on every single print) and, if that succeeds, is
    persisted straight back onto the order - so this self-heals once and
    every later print/export for that order is instant again, same as an
    order that synced cleanly the first time.

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
    "{Ship-from CompCode}-{Sale_Noc}-{session}".

    Sep 18 2026, user's explicit ask ("add sap delivery challan number
    ex: P8D1-240 in the print pdf and excel also"): once SAP has
    actually turned this order's Outbound Delivery Request into a real
    Outbound Delivery, looks up + caches that delivery's own
    human-readable ID (e.g. "P8D1-240" - see sap_outbound_delivery_
    client.find_outbound_delivery_ids) onto the order as
    `outbound_delivery_display_ids`, so it only needs one live SAP
    round-trip ever, same self-healing-cache pattern as `gi_delivery_
    request_id` above. Blank/None (never blocks the print) until SAP
    has actually produced a Delivery for this order - there's nothing
    to show before that."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise StockTransferOrderNotFoundError(f"Stock Transfer Order {sto_id} not found.")

    outbound_delivery_display_ids = doc.get("outbound_delivery_display_ids")
    if not outbound_delivery_display_ids and sap_outbound_delivery_client is not None and doc.get("sap_order_uuid"):
        try:
            delivery_items = sap_outbound_delivery_client.find_delivery_request_items(doc["sap_order_uuid"])
            item_uuids = [it["uuid"] for it in delivery_items if it.get("uuid")]
            if item_uuids:
                outbound_delivery_display_ids = sap_outbound_delivery_client.find_outbound_delivery_ids(item_uuids)
                if outbound_delivery_display_ids:
                    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"outbound_delivery_display_ids": outbound_delivery_display_ids}})
        except Exception as e:
            logger.warning(f"Delivery note {sto_id}: SAP Outbound Delivery number lookup failed, printing without it: {e}")

    live_rates = {}
    missing_price_product_ids = [it["product_id"] for it in doc["items"] if not (it.get("rate") or 0)]
    if missing_price_product_ids and sap_valuation_client:
        try:
            product_uuid_by_id = {
                c["_id"]: c.get("product_uuid")
                for c in db["component_master"].find({"_id": {"$in": missing_price_product_ids}}, {"product_uuid": 1})
            }
            product_uuids = [u for u in product_uuid_by_id.values() if u]
            costs = _get_daily_cached_costs(db, sap_valuation_client, product_uuids, doc["ship_from_site_id"]) if product_uuids else {}
            for pid, product_uuid in product_uuid_by_id.items():
                cost = costs.get(product_uuid.upper()) if product_uuid else None
                if cost:
                    live_rates[pid] = cost["amount"]
        except Exception as e:
            logger.warning(f"Delivery note {sto_id}: live SAP price fallback failed for {missing_price_product_ids}: {e}")

    items = []
    stored_items = []
    items_updated = False
    total_amount = 0.0
    for item in doc["items"]:
        rate = item.get("rate") or 0.0
        qty = item["requested_qty"]
        amount = item.get("amount")
        if not rate and item["product_id"] in live_rates:
            rate = live_rates[item["product_id"]]
            amount = round(rate * qty, 3)
            items_updated = True
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
        stored_items.append({**item, "rate": rate, "amount": amount})

    if items_updated:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"items": stored_items}})

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
    # Sep 3 2026: prefer the CompCode actually synced to the ERP
    # (erp_comp_code) over ship_from_site_id - only present on orders
    # synced after the P1W/P2W fix above; older orders fall back to the
    # raw site ID exactly as before (unchanged).
    serial_prefix = doc.get("erp_comp_code") or doc["ship_from_site_id"]
    serial_number = f"{serial_prefix}-{erp_sale_noc}-{session}" if erp_sale_noc and session else (str(erp_sale_noc) if erp_sale_noc else None)

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
        "remark": doc.get("remark"),
        "sap_outbound_delivery_no": ", ".join(outbound_delivery_display_ids) if outbound_delivery_display_ids else None,
        "items": items,
        "total_amount": round(total_amount, 2),
    }
