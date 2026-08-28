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

Confirmed live (Aug 28 2026): calling `release_delivery` before `PGRBackground` was the WRONG sequence and appears to have permanently broken P8D1-192/193 into a stuck "action is disabled" state on BOTH actions. The native SAP "Post Goods Receipt" screen (a fresh, never-Released Notification, e.g. P8D1-172) succeeds by calling PGR Ground DIRECTLY - no Release step - and creates a real Confirmed Inbound Delivery + posts real stock. `receive_stock_transfer_order` below therefore calls `post_goods_receipt` directly, with no Release call, matching the native UI's own path."""
import logging
from datetime import datetime, timezone

from sap_inbound_delivery_client import SAPInboundDeliveryError

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


def receive_stock_transfer_order(db, sap_inbound_delivery_client, sto_id: str, actor: str, quantity_overrides: dict = None) -> dict:
    """`quantity_overrides`: optional {str(line_no): received_qty} for
    any line the user edited away from the default full quantity. Every
    line not present here receives in full, exactly as SAP's own Inbound
    Delivery Notification already shows.

    Best-effort per delivery: one failed line's SAP error doesn't stop
    the others from being received - matches this app's existing Goods
    Issue pattern. Returns {status: "received"|"partial"|"failed",
    results: [...]}."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") != "posted":
        raise ValueError("This order's Goods Issue hasn't posted in SAP yet - nothing to receive.")
    if doc.get("receipt_status") == "received":
        return {"status": "received", "results": doc.get("receipt_results") or []}

    quantity_overrides = quantity_overrides or {}
    lines_by_product = {}
    for it in (doc.get("items") or []):
        lines_by_product.setdefault(it["product_id"], []).append(it)

    results = []
    any_failed = False
    for delivery_id in (doc.get("outbound_delivery_ids") or []):
        try:
            delivery = sap_inbound_delivery_client.find_delivery_by_id(delivery_id)
            if not delivery:
                results.append({"delivery_id": delivery_id, "status": "waiting", "error": "Inbound Delivery Notification not yet visible in SAP - try again shortly."})
                any_failed = True
                continue
            for item in delivery["items"]:
                sto_lines = lines_by_product.get(item["product_id"]) or []
                sto_line = sto_lines.pop(0) if sto_lines else None
                override = quantity_overrides.get(str(sto_line["line_no"])) if sto_line else None
                if override is not None and abs(float(override) - item["quantity"]) > 1e-6:
                    sap_inbound_delivery_client.update_item_quantity(item["quantity_object_id"], float(override))
            sap_inbound_delivery_client.post_goods_receipt(delivery["object_id"])
            results.append({"delivery_id": delivery_id, "status": "received"})
        except SAPInboundDeliveryError as e:
            logger.error(f"Inbound receipt failed for {sto_id} / delivery {delivery_id}: {e}")
            results.append({"delivery_id": delivery_id, "status": "failed", "error": str(e)})
            any_failed = True

    overall = "failed" if all(r["status"] != "received" for r in results) else ("partial" if any_failed else "received")
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "receipt_status": overall,
        "receipt_results": results,
        "receipt_error": " | ".join(f"{r['delivery_id']}: {r.get('error')}" for r in results if r.get("error")) or None,
        "received_at": datetime.now(timezone.utc) if overall == "received" else doc.get("received_at"),
        "received_by": actor,
    }})
    return {"status": overall, "results": results}
