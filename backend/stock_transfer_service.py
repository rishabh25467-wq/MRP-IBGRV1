"""SAP Business ByDesign Inter-Plant Stock Transfer Order (STO) screen
backend (Aug 2026).

Real SAP write happens via a specific web service, "ManageCustomerRequirementIn"
(Customer Requirement Processing process component) - confirmed via SAP's own
public docs, header fields ShipFromSiteID/ShipToSiteID/ShipToLocationID with
line items under ExternalRquestItem. The user's SAP Basis team has NOT yet
activated/exposed this web service on this tenant (confirmed directly by the
user, Aug 2026) - so this module only builds and validates STOs and persists
them locally with status "pending_sap". Wiring the actual SAP write is future
work once Basis exposes the endpoint (see how every other write in this app
is configured via SAP_SOAP_*/SAP_ODATA_* in backend/.env + server.py).

Reuses the SAME Site -> Company mapping already used for WIP Clearing
(sap_wip_clearing_client.SITE_TO_COMPANY) for the "Ship-to Site must be in
the same Company as Ship-from Site" rule - the user explicitly confirmed
(Aug 2026) that mapping is correct for this feature too, over the spec's own
illustrative (and not accurate for this tenant) example."""
import json
import logging
import re
import uuid
from datetime import datetime, timezone, date

from pymongo import ReturnDocument

from sap_wip_clearing_client import company_and_set_of_books_for_site
from production_confirmation_service import is_usable_stock_status
from inventory_service import list_known_sites

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


def get_product_stock_locations(db, product_id: str) -> dict:
    """Every USABLE, in-stock location this product currently sits in,
    across every site/warehouse - backs both the "Check Inventory" step and
    the "Select Source Warehouse" dropdown. Purely cache-based
    (inventory_cache, refreshed every couple hours - the same source
    InventoryPage.js already reads), no live SAP call - this is a fast,
    frequent lookup as the user searches/picks items, not a one-off
    pre-write sufficiency gate like check_component_availability."""
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
        if qty <= 0 or not is_usable_stock_status(loc.get("stock_status"), loc.get("restricted", False)):
            continue
        warehouse_id = _warehouse_id_from_logistics_area_id(loc.get("logistics_area_id"))
        if not warehouse_id:
            continue
        locations.append({
            "site_id": _site_id_from_full_site(loc.get("site")),
            "warehouse_id": warehouse_id,
            "warehouse_name": loc.get("logistics_area"),
            "qty": qty,
        })
    locations.sort(key=lambda l: -l["qty"])
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
        # Populated once the real SAP write is wired in and SAP rejects an
        # order (e.g. a site/warehouse master-data mismatch on SAP's side)
        # - always None today since there is no live write yet (see module
        # docstring). Shown verbatim, human-readable, in the order's detail
        # modal - never a raw stack trace/exception repr.
        "error_message": None,
        "created_by": created_by,
        "created_at": now,
        "ship_from_site_id": ship_from_site_id,
        "ship_to_site_id": ship_to_site_id,
        "ship_to_location_id": ship_to_location_id,
        "ship_to_location_name": ship_to_warehouses.get(ship_to_location_id),
        "delivery_priority": "Immediate",
        "requested_delivery_date": requested_delivery_date,
        "items": resolved_items,
    }
    db[STO_COLLECTION].insert_one(sto_doc)
    return sto_doc


def list_stock_transfer_orders(db, limit: int = 100) -> list:
    return list(db[STO_COLLECTION].find({}).sort("created_at", -1).limit(limit))


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
