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
build_line_overrides below).

Sep 2026 UPDATE - superseded by start_automated_receipt below: the
Playwright path above is dead. `khinbounddelivery`/
`khinbounddeliveryexecution` (custom OData services) now drive
Acknowledge/Release/ConfirmAsPlanned/PostGoodsReceipt directly, no
browser automation, no background job/webhook/polling - see
start_automated_receipt's own comment block for the exact synchronous
chain (user's explicit architectural mandate)."""
import logging
import re
from datetime import datetime, timezone

import job_store
from sap_wip_clearing_client import company_and_set_of_books_for_site, inbound_staging_area_for_site
from store_approval_service import _trigger_goods_movement

logger = logging.getLogger(__name__)

STO_COLLECTION = "stock_transfer_orders"
_PENDING_QUERY = {
    "gi_status": "posted",
    "receipt_status": {"$nin": ["received"]},
}

# Sep 2026, corrected placement (moved off the Outbound "Complete STO
# Process" button where it was mistakenly wired at first - see
# /app/memory/STO_CONTEXT.md): every site's Material Flow Destination
# rule routes an incoming STO into that site's own "{SITE}-HOLD"
# staging warehouse regardless of the real target warehouse picked at
# STO creation. Once the actual SAP Goods Receipt has posted via the
# "Receive" button above, this moves the received quantity from
# "{SITE}-HOLD" to that real target warehouse (`ship_to_location_id`).
#
# Sep 20 2026, user's explicit ask ("we changed logistic model in P8
# Destination set: P8-RM (Target Area) so you need moved stock from the
# RM warehouse") - P8 no longer stages through "P8-HOLD" at all; SAP now
# routes its Goods Receipts straight into "P8-RM". Delegates to the
# shared per-site override in sap_wip_clearing_client (also used by
# supplier_shipment_service's GRN flow) instead of blindly assuming
# "-HOLD" for every site.
def _receipt_hold_warehouse_id(site_id: str) -> str:
    return inbound_staging_area_for_site(site_id)


def _relocate_receipt_from_hold(db, sap_goods_movement_client, doc: dict, quantity_overrides: dict = None) -> dict:
    """Called right after a Receive job's finalize_receipt confirms the
    real SAP Goods Receipt posted (received or partial - never for a
    fully failed receipt, nothing landed in hold to move). Attempts
    the SAP Goods Movement directly and trusts SAP's own live rejection
    for a genuinely missing/insufficient line - a real incident
    (STO-000100, Sep 2026) proved a LOCAL stock pre-check here was
    wrong: it read the `inventory_cache` collection (refreshed only
    every couple of hours per stock_transfer_service.get_product_stock_locations),
    which hadn't caught up yet seconds after the real Goods Receipt
    landed the stock in hold - so 2 of 3 genuinely-received lines
    were wrongly blocked as "no stock" while SAP itself already had it.
    SAP's own "negative stock not permitted" rejection (real-time, the
    actual source of truth) is mapped to the user-facing "Stock does
    not exist in the STO warehouse" instead. Never raises - failure
    here must never undo the already-successful receipt; caller stores
    the result.

    Sep 21 2026, user's explicit ask: this is now THE receive action
    itself (see receive_stock_transfer_order below), not just a
    post-Playwright cleanup step, so it accepts an optional
    `quantity_overrides` ({str(line_no): qty}) the same way the old
    Playwright grid override did."""
    site_id = doc.get("ship_to_site_id")
    hold_warehouse_id = _receipt_hold_warehouse_id(site_id)
    ship_to_location_id = doc.get("ship_to_location_id")
    if not ship_to_location_id or ship_to_location_id == hold_warehouse_id:
        return {"status": "skipped_same_warehouse"}
    items = doc.get("items") or []
    if not items:
        return {"status": "skipped_no_items"}
    quantity_overrides = quantity_overrides or {}
    owner_party_id, _ = company_and_set_of_books_for_site(site_id)
    line_results = []
    for item in items:
        qty = quantity_overrides.get(str(item["line_no"]), item["requested_qty"])
        result = _trigger_goods_movement(
            sap_goods_movement_client, owner_party_id, item["product_id"],
            hold_warehouse_id, ship_to_location_id,
            qty, item.get("unit_of_measure") or "EA", site_id,
        )
        if not result.get("ok"):
            raw = result.get("error_detail") or result.get("error") or ""
            if re.search(r"negative stock not permitted", raw, re.IGNORECASE):
                error = "Stock does not exist in the STO warehouse"
            else:
                error = result.get("error") or raw or "unknown SAP error"
            line_results.append({"product_id": item["product_id"], "ok": False, "error": error})
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
    if doc.get("receipt_status") not in ("received", "partial", "failed"):
        raise ValueError("This order must be received before retrying the warehouse move.")
    result = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc)
    # Sep 21 2026 fix (real live incident, STO-000132) - this used to
    # ONLY update `receipt_relocation`, leaving `receipt_status`/
    # `receipt_error` frozen on whatever they were from the ORIGINAL
    # failed/partial attempt - so a genuinely-fixed order kept showing
    # up in the Pending tab as "Receipt Failed" with a stale error
    # message forever, even after this retry fully succeeded.
    status_map = {"done": "received", "partial": "partial", "failed": "failed"}
    overall = status_map.get(result.get("status"), doc.get("receipt_status"))
    lines = result.get("lines") or []
    error_summary = " | ".join(f"{l['product_id']}: {l['error']}" for l in lines if not l["ok"]) or None
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "receipt_relocation": result, "receipt_status": overall, "receipt_error": error_summary,
        "receipt_results": lines or doc.get("receipt_results"),
    }})
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


def _verify_inbound_delivery_notification(db, sap_inbound_delivery_client, doc: dict) -> list:
    """Sep 22 2026, user's explicit ask ("stock existence shouldn't be
    the check") - re-fetches each outbound delivery ID FROM THE INBOUND
    SIDE (`InboundDeliveryCollection` at the receiving site) instead of
    only trusting our own `gi_status`/`outbound_delivery_ids` (the
    SHIPPING side's own records). The Notification's own `ID` is
    identical to the Outbound Delivery ID that produced it (confirmed
    live), so this just re-fetches the SAME id string through the
    INBOUND OData service (`sap_inbound_delivery_client.py`,
    "provisioned, not currently wired into any live flow" until now) and
    only trusts it once SAP itself confirms the document + item
    quantities genuinely exist there too. Cached on the doc
    (`inbound_delivery_ids`) once verified - never re-checked once set.
    Returns [] (never raises) if SAP hasn't created it yet - callers
    decide whether that's fatal."""
    if doc.get("inbound_delivery_ids"):
        return doc["inbound_delivery_ids"]
    if not sap_inbound_delivery_client:
        return []
    verified_ids = []
    for delivery_id in doc.get("outbound_delivery_ids") or []:
        try:
            found = sap_inbound_delivery_client.find_delivery_by_id(delivery_id)
        except Exception as e:
            logger.warning(f"Inbound Delivery Notification verification failed for {delivery_id}: {e}")
            continue
        if found:
            verified_ids.append(found["id"])
    if verified_ids:
        db[STO_COLLECTION].update_one({"_id": doc["_id"]}, {"$set": {"inbound_delivery_ids": verified_ids}})
    return verified_ids


def list_pending_receipts(db, sap_outbound_delivery_client, sap_inbound_delivery_client=None, site_id: str = None) -> list:
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
        # Sep 22 2026 - best-effort, cached after the first success (see
        # `_verify_inbound_delivery_notification` above) so this only
        # costs a live SAP call once per order, not on every page load.
        inbound_delivery_ids = _verify_inbound_delivery_notification(db, sap_inbound_delivery_client, doc)
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
            "inbound_delivery_ids": inbound_delivery_ids,
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


def prepare_receipt(db, sto_id: str, sap_inbound_delivery_client=None) -> dict:
    """Validates the order is receivable and returns its doc - raises
    ValueError (400 to the caller) for any invalid state."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") != "posted":
        raise ValueError("This order's Goods Issue hasn't posted in SAP yet - nothing to receive.")
    # Sep 21 2026, user's explicit ask - Receive only ever posts a real
    # SAP Goods Movement now (see receive_stock_transfer_order below),
    # never drives Playwright/logs into SAP UI - this needs a real SAP
    # delivery reference to exist first (`outbound_delivery_ids`, set
    # once Goods Issue posts, same gate try_post_goods_issue itself waits
    # on) so it never fires against a half-created order.
    if not doc.get("outbound_delivery_ids"):
        raise ValueError("No SAP delivery reference found yet for this order - please retry in a moment.")
    if not doc.get("items"):
        raise ValueError(f"Stock Transfer Order {sto_id} has no line items - nothing to receive.")
    # Sep 22 2026, user's explicit ask ("stock existence shouldn't be the
    # check") - real incident (STO-000137, P8 override bug) showed
    # `gi_status`/`outbound_delivery_ids` alone (the SHIPPING side's own
    # records) aren't proof the RECEIVING side has anything to act on
    # yet. Block Receive until SAP itself confirms the Inbound Delivery
    # Notification genuinely exists at the receiving site - see
    # `_verify_inbound_delivery_notification` above.
    inbound_delivery_ids = _verify_inbound_delivery_notification(db, sap_inbound_delivery_client, doc)
    if sap_inbound_delivery_client and not inbound_delivery_ids:
        raise ValueError(
            "SAP hasn't created the Inbound Delivery Notification at the receiving site yet - please try again in a moment."
        )
    doc["inbound_delivery_ids"] = inbound_delivery_ids
    return doc
    return doc


def receive_stock_transfer_order(db, sap_goods_movement_client, sto_id: str, actor: str, quantity_overrides: dict = None) -> dict:
    """Sep 21 2026, user's explicit ask - STOP driving Playwright/SAP UI
    login entirely for STO receiving: too slow (SAP's own UI took
    40-90s/delivery) and its own "Post Goods Receipt" -> "Save and
    Close" -> re-search verification proved unreliable on a real
    2-line order (STO-000131/P1D1-568: "still shows as Not Released"
    after a clean Save and Close, no visible SAP error). The "Receive"
    button now ONLY posts a real SAP Goods Movement (`_trigger_goods_
    movement`, plain OData API call, no login, no browser) moving each
    line's quantity from wherever Goods Receipt lands ({SITE}-RM/-HOLD
    - see inbound_staging_area_for_site) to the STO's real destination
    (`ship_to_location_id`) - see _relocate_receipt_from_hold, now
    called directly as the receive action itself rather than as a
    post-Playwright cleanup step. Requires `prepare_receipt`'s SAP
    delivery reference gate to have already passed.

    Sep 22 2026: also the final step of the new automated GR flow (see
    start_automated_receipt/complete_automated_receipt_for_lot below) -
    called once every one of the STO's deliveries has a real SAP Goods
    Receipt posted (either immediately on Release, or after the
    webhook-driven ConfirmAsPlanned). `quantity_overrides` falls back to
    whatever was captured at Receive-click time (`receipt_quantity_
    overrides`) when the caller doesn't pass any - the webhook handler
    fires minutes later and never had the original request's overrides
    in hand."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}) or {}
    if quantity_overrides is None:
        quantity_overrides = doc.get("receipt_quantity_overrides") or {}
    now = datetime.now(timezone.utc)
    started_at = doc.get("receipt_started_at")
    if started_at and started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    relocation = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc, quantity_overrides)
    status_map = {
        "done": "received", "partial": "partial", "failed": "failed",
        "skipped_same_warehouse": "received", "skipped_no_items": "received",
    }
    overall = status_map.get(relocation.get("status"), "failed")
    lines = relocation.get("lines") or []
    error_summary = " | ".join(f"{l['product_id']}: {l['error']}" for l in lines if not l["ok"]) or None
    update = {
        "receipt_status": overall,
        "receipt_results": lines,
        "receipt_relocation": relocation,
        "receipt_error": error_summary,
        "received_at": now if overall in ("received", "partial") else doc.get("received_at"),
        "received_by": actor,
        "receipt_completed_at": now,
        "receipt_duration_seconds": round((now - started_at).total_seconds()) if started_at else None,
    }
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": update})
    return {"status": overall, "results": lines, "error": error_summary, "receipt_relocation": relocation}


# Sep 22 2026 (LATE, this session) - MAJOR CORRECTION to everything
# above: the entire "Release-first, then Lot/Warehouse-Order" design
# was based on a FLAWED prior test where Release was always called
# before PostGoodsReceipt, and SAP's own action gating then disabled
# PGRBackground - misread as "PGRBackground needs Release first" /
# "this site is task-based". Live-proven this session on 2 BRAND NEW,
# never-touched STOs (one at P1, one at P8 - the exact two sites
# earlier assumed to be task-based vs non-task-based): calling
# Acknowledge -> PostGoodsReceipt DIRECTLY, with NO Release call at
# all, succeeds immediately and posts REAL inventory (confirmed via
# SAPInventoryClient: P8-HOLD +1 KGM, P1-HOLD +1 EA, exactly the
# shipped qty, both times) - SAP itself flips ReleaseStatusCode to
# Released as a side effect. No Warehouse Order / Site Logistics Lot /
# task chain is involved at all for a fresh delivery. `KBA 3583076`
# ("PGRBackground disabled") only reproduces once a delivery has
# ALREADY been Released without an immediate GR (a real, separate,
# already-known-dead-end state - see STO-000123/P1D1-560) - it is NOT
# a blanket rule, and Release should NEVER be called proactively by
# this engine anymore.
#
# New order, one pass per delivery, zero polling/waiting anywhere:
#   Acknowledge -> (quantity overrides, if any) -> PostGoodsReceipt
#   directly. If that succeeds, done - real GR posted, no Release, no
#   Lot involved.
#   If PostGoodsReceipt is rejected (a genuinely already-released-but-
#   unfinished delivery, or some other tenant-specific block): fall
#   back to the old Release -> immediate Lot lookup -> ConfirmAsPlanned
#   route as a SECOND attempt, still in the same synchronous pass. If
#   that also finds nothing, raise immediately - the receive request
#   fails and the user retries manually a moment later. Never parks,
#   never waits, never retries in a loop.
_SAP_FINISHED_STATUS_CODE = "3"


def _apply_quantity_overrides(sap_inbound_delivery_client, delivery_id: str, line_overrides: dict) -> None:
    """`line_overrides`: {product_id: qty} (see build_line_overrides) -
    PATCHes each item's InboundDeliveryItemQuantity row BEFORE
    PostGoodsReceipt, the only lever this OData service exposes for a
    non-default quantity (see sap_inbound_delivery_client.py module
    docstring)."""
    if not sap_inbound_delivery_client or not line_overrides:
        return
    try:
        delivery = sap_inbound_delivery_client.find_delivery_by_id(delivery_id)
    except Exception as e:
        logger.warning(f"Could not fetch delivery {delivery_id} for quantity override: {e}")
        return
    for item in (delivery or {}).get("items") or []:
        override_qty = line_overrides.get(item["product_id"])
        if override_qty is not None and item.get("quantity_object_id"):
            try:
                sap_inbound_delivery_client.update_item_quantity(item["quantity_object_id"], override_qty)
            except Exception as e:
                logger.warning(f"Could not override quantity for {delivery_id}/{item['product_id']}: {e}")


def _find_matching_lot(sap_execution_client, site_id: str, product_ids: list) -> dict:
    """Single immediate lookup (see sap_inbound_delivery_execution_client.
    find_recent_lots) - no retry, no wait. Matches by exact site + product
    set, same heuristic the old webhook matching used. Only reached now
    as a FALLBACK when direct PostGoodsReceipt (the new primary path)
    itself gets rejected - see start_automated_receipt."""
    if not sap_execution_client:
        return None
    try:
        candidates = sap_execution_client.find_recent_lots(limit=50)
    except Exception as e:
        logger.warning(f"Immediate Warehouse Order lookup failed: {e}")
        return None
    target = sorted(product_ids)
    for lot in candidates:
        if lot.get("site_id") == site_id and sorted(lot.get("products") or []) == target:
            return lot
    return None


def start_automated_receipt(
    db, sap_kh_inbound_delivery_client, sap_inbound_delivery_client, sap_execution_client,
    sap_goods_movement_client, sto_id: str, actor: str, quantity_overrides: dict = None,
) -> dict:
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    delivery_ids = doc.get("inbound_delivery_ids") or doc.get("outbound_delivery_ids") or []
    site_id = doc.get("ship_to_site_id")
    # This tenant's per-delivery product split isn't guaranteed 1:1 with
    # STO lines (a single delivery has carried multiple products in
    # live testing) - use the full line-item product set already on the
    # STO doc for matching rather than re-querying SAP (also sidesteps
    # `khinbounddelivery`'s different item-navigation schema).
    all_product_ids = sorted({it["product_id"] for it in (doc.get("items") or [])})
    line_overrides = build_line_overrides(doc, quantity_overrides or {})
    for delivery_id in delivery_ids:
        try:
            found = sap_kh_inbound_delivery_client.find_object_id(delivery_id)
        except Exception as e:
            raise ValueError(f"Could not reach SAP to look up delivery {delivery_id}: {e}")
        if not found:
            continue
        if found.get("delivery_processing_status_code") == _SAP_FINISHED_STATUS_CODE:
            continue  # already received (e.g. a repeat click) - nothing to do
        try:
            sap_kh_inbound_delivery_client.acknowledge_delivery_note_receipt(found["object_id"])
        except Exception as e:
            logger.info(f"Acknowledge for {delivery_id} ({sto_id}) skipped/failed (may already be acknowledged): {e}")
        _apply_quantity_overrides(sap_inbound_delivery_client, delivery_id, line_overrides)
        pgr_error = None
        try:
            sap_inbound_delivery_client.post_goods_receipt(found["object_id"])
        except Exception as e:
            pgr_error = e
        if pgr_error is None:
            continue  # Path A: direct PostGoodsReceipt succeeded, real GR posted
        # Path B fallback: this specific delivery rejected direct PGR
        # (e.g. already Released-but-unfinished from an earlier attempt -
        # see STO-000123/P1D1-560) - try Release -> immediate Lot lookup.
        try:
            sap_kh_inbound_delivery_client.release_delivery(found["object_id"])
        except Exception as e:
            logger.info(f"Release for {delivery_id} ({sto_id}) skipped/failed (may already be released): {e}")
        refreshed = sap_kh_inbound_delivery_client.find_object_id(delivery_id) or found
        if refreshed.get("delivery_processing_status_code") == _SAP_FINISHED_STATUS_CODE:
            continue  # Release itself finished it
        lot = _find_matching_lot(sap_execution_client, site_id, all_product_ids)
        if lot:
            for activity in lot.get("activities") or []:
                if activity.get("status_code") != _SAP_FINISHED_STATUS_CODE:
                    sap_execution_client.confirm_activity_as_planned(activity["object_id"])
            continue
        raise ValueError(
            f"SAP hasn't finished processing delivery {delivery_id} yet - direct Goods Receipt was rejected "
            f"({pgr_error}) and no Warehouse Order was found either. Please retry the receipt in a moment."
        )
    return receive_stock_transfer_order(db, sap_goods_movement_client, sto_id, actor, quantity_overrides)


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
    "{SITE}-HOLD" -> real target warehouse Goods Movement for any
    ship-to site - see _relocate_receipt_from_hold."""
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
    if overall in ("received", "partial") and doc.get("ship_to_site_id") and sap_goods_movement_client:
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
