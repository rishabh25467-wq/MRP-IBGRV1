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

import job_store
import stock_transfer_service
from sap_wip_clearing_client import company_and_set_of_books_for_site
from store_approval_service import _trigger_goods_movement

logger = logging.getLogger(__name__)

STO_COLLECTION = "stock_transfer_orders"
_PENDING_QUERY = {
    "gi_status": "posted",
    "receipt_status": {"$nin": ["received"]},
}

# Sep 2026, corrected placement (moved off the Outbound "Complete STO
# Process" button where it was mistakenly wired at first - see
# /app/memory/STO_CONTEXT.md): Site P8's Material Flow Destination rule
# routes every incoming STO into P8-HOLD (a neutral staging area)
# regardless of the real target warehouse picked at STO creation. Once
# the actual SAP Goods Receipt has posted via the "Receive" button
# above, this moves the received quantity from P8-HOLD to that real
# target warehouse (`ship_to_location_id`).
RECEIPT_RELOCATION_SITE_ID = "P8"
RECEIPT_RELOCATION_HOLD_WAREHOUSE_ID = "P8-HOLD"


def _relocate_receipt_from_hold(db, sap_goods_movement_client, doc: dict) -> dict:
    """Called right after a Receive job's finalize_receipt confirms the
    real SAP Goods Receipt posted (received or partial - never for a
    fully failed receipt, nothing landed in P8-HOLD to move). Checks
    live stock in P8-HOLD per line first - user's explicit ask - a line
    that genuinely never received (failed delivery within a partial
    receipt) naturally has no stock there yet, surfaced as a per-line
    "Stock does not exist in the STO warehouse" error rather than a
    confusing SAP rejection. Never raises - failure here must never
    undo the already-successful receipt; caller stores the result."""
    ship_to_location_id = doc.get("ship_to_location_id")
    if not ship_to_location_id or ship_to_location_id == RECEIPT_RELOCATION_HOLD_WAREHOUSE_ID:
        return {"status": "skipped_same_warehouse"}
    items = doc.get("items") or []
    if not items:
        return {"status": "skipped_no_items"}
    owner_party_id, _ = company_and_set_of_books_for_site(RECEIPT_RELOCATION_SITE_ID)
    line_results = []
    for item in items:
        stock = stock_transfer_service.get_product_stock_locations(db, item["product_id"])
        location = next((l for l in stock["locations"] if l["warehouse_id"] == RECEIPT_RELOCATION_HOLD_WAREHOUSE_ID), None)
        if location is None or location["qty"] + 1e-6 < item["requested_qty"]:
            line_results.append({"product_id": item["product_id"], "ok": False, "error": "Stock does not exist in the STO warehouse"})
            continue
        result = _trigger_goods_movement(
            sap_goods_movement_client, owner_party_id, item["product_id"],
            RECEIPT_RELOCATION_HOLD_WAREHOUSE_ID, ship_to_location_id,
            item["requested_qty"], item.get("unit_of_measure") or "EA", RECEIPT_RELOCATION_SITE_ID,
        )
        if not result.get("ok"):
            line_results.append({"product_id": item["product_id"], "ok": False, "error": result.get("error") or result.get("error_detail") or "unknown SAP error"})
        else:
            line_results.append({"product_id": item["product_id"], "ok": True, "gac_id": result.get("external_id")})
    all_ok = all(r["ok"] for r in line_results)
    any_ok = any(r["ok"] for r in line_results)
    status = "done" if all_ok else ("partial" if any_ok else "failed")
    return {"status": status, "to": ship_to_location_id, "lines": line_results}


def retry_receipt_relocation(db, sap_goods_movement_client, sto_id: str) -> dict:
    """Retry button on the Inbound Receipts (Completed tab) page for
    when _relocate_receipt_from_hold failed or partially failed after a
    successful "Receive"."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("receipt_status") not in ("received", "partial"):
        raise ValueError("This order must be received before retrying the warehouse move.")
    if doc.get("ship_to_site_id") != RECEIPT_RELOCATION_SITE_ID:
        raise ValueError(f"This retry only applies to Site {RECEIPT_RELOCATION_SITE_ID} destinations.")
    result = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc)
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"receipt_relocation": result}})
    return result


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
    hasn't been fully received on the app side yet. Each result also
    carries its own currently-running receive job (if any) as
    `active_job` (Aug 2026, user's explicit ask - "show a symbol on
    each STO clearly so users know job is ON") so a page refresh (or a
    second tab/device) still shows the live badge instead of losing it
    the moment client-side state resets (testing_agent iteration_133
    finding)."""
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
    active_jobs = {
        j["sto_id"]: {"job_id": j["_id"], "phase": j.get("phase"), "progress_current": j.get("progress_current"), "progress_total": j.get("progress_total"), "processing_started_at": j.get("processing_started_at")}
        for j in db[job_store.COLLECTION_NAME].find({"sto_id": {"$in": [r["sto_id"] for r in results]}, "kind": "inbound_receipt", "status": "running"})
    }
    for r in results:
        r["active_job"] = active_jobs.get(r["sto_id"])
    return results


def list_ship_to_sites_with_pending_receipts(db) -> list:
    """Distinct ship-to sites across every pending receipt - backs the
    page's site filter dropdown."""
    return sorted(s for s in db[STO_COLLECTION].distinct("ship_to_site_id", _PENDING_QUERY) if s)


def list_completed_receipts(db, site_id: str = None, date_from: datetime = None, date_to: datetime = None) -> list:
    """Every STO that's had a receive attempt actioned (received,
    partially received, or failed) - backs the page's new "Completed"
    tab (user's explicit ask, Aug 2026: "a filter to be able to see
    receipts also in a table with the time taken to complete receipt in
    SAP"). Filtered by ship-to site and a `receipt_completed_at` date
    range. `receipt_completed_at` was one-time backfilled (Aug 28 2026,
    see /app/memory/CHANGELOG.md) for every STO that predates this
    field, so the date filter matches historical receipts too.
    `receipt_duration_seconds` stays None for any of those pre-existing
    STOs (no `receipt_started_at` was ever recorded for them) - only
    receipts actioned from Aug 28 2026 onward get a real duration."""
    query = {"receipt_status": {"$in": ["received", "partial", "failed"]}}
    if site_id:
        query["ship_to_site_id"] = site_id
    if date_from or date_to:
        date_range = {}
        if date_from:
            date_range["$gte"] = date_from
        if date_to:
            date_range["$lte"] = date_to
        query["receipt_completed_at"] = date_range
    docs = list(db[STO_COLLECTION].find(query).sort("receipt_completed_at", -1).limit(300))
    return [{
        "sto_id": doc["_id"],
        "sap_order_id": (doc.get("sap_order_id") or "").lstrip("0") or doc.get("sap_order_id"),
        "ship_from_site_id": doc.get("ship_from_site_id"),
        "ship_to_site_id": doc.get("ship_to_site_id"),
        "ship_to_location_name": doc.get("ship_to_location_name"),
        "receipt_status": doc.get("receipt_status"),
        "receipt_error": _humanize_sap_error(doc.get("receipt_error")),
        "received_at": doc.get("received_at"),
        "received_by": doc.get("received_by"),
        "receipt_completed_at": doc.get("receipt_completed_at"),
        "receipt_duration_seconds": doc.get("receipt_duration_seconds"),
        "items_count": len(doc.get("items") or []),
        "receipt_relocation": doc.get("receipt_relocation"),
    } for doc in docs]


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


def finalize_receipt(db, sto_id: str, results: list, actor: str, had_overrides: bool = False, sap_username: str = None, sap_goods_movement_client=None) -> dict:
    """Persists the final receipt outcome after the Playwright PGR run -
    same status rollup this feature has always used. Also stamps
    `receipt_completed_at` and, when `receipt_started_at` was recorded
    (set right when the job was created - see post_inbound_receipt),
    `receipt_duration_seconds` - how long the automation itself took for
    THIS attempt (user's explicit ask, Aug 2026: "time taken to complete
    receipt in SAP"). Recomputed fresh on every attempt (including
    retries after a failure), so a retry's duration reflects just that
    retry's run, not cumulative time since the very first attempt.

    Sep 12 2026, user's explicit ask ("which user you used while
    receiving...in SAP") - `sap_username` stamps which pooled SAP UI
    login (see playwright_concurrency.py) actually ran THIS attempt.

    Sep 2026: once the receipt genuinely lands stock (received or
    partial - never on a full failure), immediately triggers the
    P8-HOLD -> real target warehouse Goods Movement for Site P8
    destinations - see _relocate_receipt_from_hold."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}) or {}
    any_failed = any(r["status"] != "received" for r in results)
    overall = "failed" if all(r["status"] != "received" for r in results) else ("partial" if any_failed else "received")
    error_summary = " | ".join(f"{r['delivery_id']}: {_humanize_sap_error(r.get('error'))}" for r in results if r.get("error")) or None
    now = datetime.now(timezone.utc)
    started_at = doc.get("receipt_started_at")
    if started_at and started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    update = {
        "receipt_status": overall,
        "receipt_results": results,
        "receipt_error": error_summary,
        "receipt_sap_username": sap_username,
        "received_at": now if overall == "received" else doc.get("received_at"),
        "received_by": actor,
        "receipt_completed_at": now,
        "receipt_duration_seconds": round((now - started_at).total_seconds()) if started_at else None,
    }
    relocation = None
    if overall in ("received", "partial") and doc.get("ship_to_site_id") == RECEIPT_RELOCATION_SITE_ID and sap_goods_movement_client:
        relocation = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc)
        update["receipt_relocation"] = relocation
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": update})
    return {"status": overall, "results": results, "had_overrides": had_overrides, "error": error_summary, "sap_username": sap_username, "receipt_relocation": relocation}


def _humanize_sap_error(raw: str) -> str:
    """SAP's raw OData fault is a JSON blob (e.g. from an older
    receipt_results entry) - pull out just the message text for display;
    Playwright-sourced errors are already plain text and pass through.
    Anything unrecognized (a raw HTTP/connection error, an untruncated
    JSON/HTML payload) is capped short rather than dumped verbatim to the
    end user (testing_agent iteration_134: a full '{"error":{"code":...'
    blob was leaking through for STO-000057)."""
    if not raw:
        return raw
    match = re.search(r'"value"\s*:\s*"([^"]+)"', raw) or re.search(r'"message"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1)
    return raw[:120] + ("…" if len(raw) > 120 else "")
