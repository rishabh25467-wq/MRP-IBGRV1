"""Inbound STO Receipt (Aug 28 2026) - lets ANY logged-in internal user
receive a Stock Transfer Order in one click instead of the SAP UI's own
Inbound Logistics screen, which shows one row PER LINE for a multi-line
STO (Goods Issue already posts one Outbound Delivery per line - see
stock_transfer_service.py's try_post_goods_issue docstring) - a real
pain point for the receiving warehouse team on a 3+ line order.

Defaults to receiving the FULL requested quantity per line (matches
what SAP's own Inbound Delivery Notification already shows, no extra
SAP call needed to know the default), but lets the user edit any
line's quantity before confirming - an edited quantity is PATCHed onto
SAP's own Inbound Delivery Item Quantity row before the Goods Receipt
(PGRBackground) is posted for that delivery, since the PGR action
itself has no quantity override (see sap_inbound_delivery_client.py).

Matches each STO line to its own Inbound Delivery Notification via the
STO's own `outbound_delivery_ids` (set once Goods Issue posts) - the
Notification's SAP `ID` is IDENTICAL to the Outbound Delivery ID that
produced it (confirmed live), so no UUID lookup dance is needed. Lines
are then matched to their own delivery's item by `product_id` (this
tenant, per line, always produces exactly one Outbound Delivery per
STO line - see stock_transfer_service.py - so this is a 1:1 match).

`outbound_delivery_ids` only exists on STOs processed since this
tracking was added (Aug 27 2026) - any older "posted" STO (e.g.
STO-000015, Aug 24 2026) still has a real, un-received Inbound Delivery
sitting in SAP, it just never recorded which delivery ID(s) resulted.
`_backfill_missing_delivery_ids` below re-derives and persists them
on-the-fly the first time list_pending_receipts is asked for such an
order, using the exact same SAP lookup try_post_goods_issue's own
release step already relies on - read-only, no SAP write.

Confirmed live (Aug 28 2026), re-tested with a clean never-touched delivery:
`PGRBackground` is genuinely disabled by SAP for this tenant's inbound
deliveries via the OData API - officially documented by SAP itself (KBA
3583076: "Inability to Post Goods Receipt with Actual Quantities via OData
API in Inbound Delivery Processing" - Actual Quantity belongs to the
Confirmed Inbound Delivery, not the Notification, and there is no
web service/API to create one). See sap_playwright_pgr_service.py's module
docstring for the full investigation (including why Site Logistics Task and
the SAP-sample "kh*" custom OData services don't help either).
`receive_stock_transfer_order_job` (server.py) therefore drives the real SAP
browser UI headlessly (Playwright) to click "Post Goods Receipt" instead -
this is unavoidably slow (SAP's own UI takes ~40-90s per delivery), so it
always runs as a background job polled by the frontend, never a synchronous
request. A quantity override is typed directly into the Playwright screen's
own "Actual Quantity" grid cell (the OData `InboundDeliveryItemQuantity`
PATCH this used to rely on is ALSO blocked tenant-wide: SAP returns
"Changing data not possible; data is read-only" - see
build_line_overrides below)."""
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STO_COLLECTION = "stock_transfer_orders"
_PENDING_QUERY = {
    "gi_status": "posted",
    "receipt_status": {"$nin": ["received"]},
}


def _backfill_missing_delivery_ids(db, sap_outbound_delivery_client, doc: dict) -> list:
    sto_id = doc["_id"]
    try:
        delivery_request_items = sap_outbound_delivery_client.find_delivery_request_items(doc["sap_order_uuid"])
        item_uuids = [d["uuid"] for d in delivery_request_items if d.get("uuid")]
        objects = sap_outbound_delivery_client.find_outbound_delivery_objects(item_uuids)
        delivery_ids = sorted({o["id"] for o in objects if o.get("id")})
    except Exception as e:
        logger.warning(f"Could not backfill outbound_delivery_ids for {sto_id}: {e}")
        return []
    if delivery_ids:
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"outbound_delivery_ids": delivery_ids}})
    return delivery_ids


def list_pending_receipts(db, sap_outbound_delivery_client, site_id: str = None) -> list:
    """Every STO whose Goods Issue has posted (so an Outbound
    Delivery/Inbound Notification genuinely exists in SAP) and that
    hasn't been fully received on the app side yet."""
    query = dict(_PENDING_QUERY)
    if site_id:
        query["ship_to_site_id"] = site_id
    docs = list(db[STO_COLLECTION].find(query).sort("created_at", -1).limit(200))
    results = []
    for doc in docs:
        delivery_ids = doc.get("outbound_delivery_ids") or []
        if not delivery_ids:
            delivery_ids = _backfill_missing_delivery_ids(db, sap_outbound_delivery_client, doc)
            if not delivery_ids:
                continue
        results.append({
            "sto_id": doc["_id"],
            "sap_order_id": (doc.get("sap_order_id") or "").lstrip("0") or doc.get("sap_order_id"),
            "ship_from_site_id": doc.get("ship_from_site_id"),
            "ship_to_site_id": doc.get("ship_to_site_id"),
            "ship_to_location_name": doc.get("ship_to_location_name"),
            "created_at": doc.get("created_at"),
            "created_by": doc.get("created_by"),
            "receipt_status": doc.get("receipt_status") or "pending",
            "receipt_error": doc.get("receipt_error"),
            "outbound_delivery_ids": delivery_ids,
            "items": [
                {
                    "line_no": it.get("line_no"),
                    "product_id": it.get("product_id"),
                    "description": it.get("description"),
                    "unit_of_measure": it.get("unit_of_measure"),
                    "requested_qty": it.get("requested_qty"),
                }
                for it in (doc.get("items") or [])
            ],
        })
    return results


def list_ship_to_sites_with_pending_receipts(db) -> list:
    """Distinct ship-to sites across every pending receipt - backs the
    page's site filter dropdown."""
    return sorted(s for s in db[STO_COLLECTION].distinct("ship_to_site_id", _PENDING_QUERY) if s)


def prepare_receipt(db, sto_id: str) -> dict:
    """Validates the order is receivable and returns its doc - raises
    ValueError (400 to the caller) for any invalid state."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") != "posted":
        raise ValueError("This order's Goods Issue hasn't posted in SAP yet - nothing to receive.")
    return doc


def build_line_overrides(doc: dict, quantity_overrides: dict) -> dict:
    """`quantity_overrides`: optional {str(line_no): received_qty} for any
    line the user edited away from the default full quantity. Returns
    {product_id: qty} for only the lines that genuinely differ from the
    full shipped amount - Playwright types these straight into SAP's own
    "Actual Quantity" grid cell (see sap_playwright_pgr_service.py); every
    other line is left to "Propose Quantities" default to its full
    Planned Quantity."""
    quantity_overrides = quantity_overrides or {}
    result = {}
    for it in (doc.get("items") or []):
        override = quantity_overrides.get(str(it["line_no"]))
        if override is not None and abs(float(override) - float(it["requested_qty"])) > 1e-6:
            result[it["product_id"]] = float(override)
    return result


def finalize_receipt(db, sto_id: str, results: list, actor: str, had_overrides: bool = False) -> dict:
    """Persists the final receipt outcome after the Playwright PGR run -
    same status rollup this feature has always used."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}) or {}
    any_failed = any(r["status"] != "received" for r in results)
    overall = "failed" if all(r["status"] != "received" for r in results) else ("partial" if any_failed else "received")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "receipt_status": overall,
        "receipt_results": results,
        "receipt_error": " | ".join(f"{r['delivery_id']}: {_humanize_sap_error(r.get('error'))}" for r in results if r.get("error")) or None,
        "received_at": datetime.now(timezone.utc) if overall == "received" else doc.get("received_at"),
        "received_by": actor,
    }})
    return {"status": overall, "results": results, "had_overrides": had_overrides}


def _humanize_sap_error(raw: str) -> str:
    """SAP's raw OData fault is a JSON blob (e.g. from an older
    receipt_results entry) - pull out just the message text for display;
    Playwright-sourced errors are already plain text and pass through."""
    if not raw:
        return raw
    match = re.search(r'"value"\s*:\s*"([^"]+)"', raw)
    return match.group(1) if match else raw
