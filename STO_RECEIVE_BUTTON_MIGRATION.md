# STO "Receive" Button — Full Migration Bundle (v2, Sep 23 2026 redesign)

Everything needed to port the Inbound STO Receipt flow into a new app. This version reflects the
Sep 23 2026 redesign: a single detail modal (fires the Goods Receipt the instant it opens, then a
separate confirmed "Warehouse Move" step) replacing the old bulk-select table UI. If you already
copied the v1 doc, this REPLACES it - the backend split (`/receive` now does ONLY the Goods
Receipt, a new `/relocate` route does the warehouse move) and the frontend (new
`ReceiptDetailModal.jsx`, simplified `InboundReceiptsPage.js`, no bulk selection) are both new.

```
Acknowledge -> PostGoodsReceipt (Path A, primary)
   -> if rejected: Release -> immediate Warehouse-Order lookup -> ConfirmAsPlanned (Path B, fallback)
[receipt_status = "awaiting_relocation" here - this is the modal's "Confirm" checkpoint]
-> Goods Movement: {SITE}-HOLD -> real target warehouse (relocation, separate step, retryable
   independently, only re-touches lines that actually failed)
```

## 1. Required `.env` variables (backend) — unchanged from v1

```env
BYD_ODATA_VHOST="my431827.businessbydesign.cloud.sap"
SAP_ODATA_INBOUND_BASE_URL="https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/inboundstockemergent"
SAP_ODATA_KH_INBOUND_DELIVERY_BASE_URL="https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/khinbounddelivery"
SAP_ODATA_INBOUND_DELIVERY_EXECUTION_BASE_URL="https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/khinbounddeliveryexecution"
SAP_ODATA_USERNAME="UNEECOPSTEAM"
SAP_ODATA_PASSWORD="<your value>"
SAP_SOAP_GOODS_MOVEMENT_ENDPOINT="https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/inventoryprocessinggoodsandac2?sap-vhost=my431827.businessbydesign.cloud.sap"
SAP_SOAP_USERNAME="_EMERGENTBOM"
SAP_SOAP_PASSWORD="<your value>"
SAP_GOODS_MOVEMENT_DRY_RUN="false"
```

## 2. Backend files — unchanged from v1, copy verbatim from that doc if you don't have them yet
`sap_rate_limiter.py`, `sap_wip_clearing_client.py`, `sap_inbound_delivery_client.py`,
`sap_inbound_delivery_execution_client.py`, `sap_goods_movement_client.py` (now with an
instance-level `requests.Session` for connection reuse - see its `__init__`), `job_store.py`.
Also still need `store_approval_service._trigger_goods_movement` and (NEW, see below)
`_clarify_goods_movement_error`.

### `store_approval_service.py` extract — UPDATED (adds a real error clarifier + warehouse name)
```python
import re

_GOODS_MOVEMENT_MAX_ATTEMPTS = 3
_GOODS_MOVEMENT_RETRY_DELAY_SECONDS = 5


def _has_sap_log_error(result: dict) -> str | None:
    xml = (result or {}).get("raw_xml") or (result or {}).get("envelope") or (result or {}).get("raw") or ""
    if not xml:
        return None
    if re.search(r"<SeverityCode>\s*[3-9]\s*</SeverityCode>", xml):
        note = re.search(r"<Note>(.*?)</Note>", xml)
        return note.group(1) if note else "SAP logged an error-severity item for this movement"
    return None


def _clarify_goods_movement_error(raw_error: str, material_id: str, warehouse_id: str = None) -> dict:
    """Translates SAP's raw Goods Movement rejection text into a plain-English message a
    warehouse user can act on - names the ACTUAL warehouse ID when known (real user feedback:
    a generic "the source warehouse" wasn't clear enough)."""
    text = raw_error or ""
    warehouse_label = warehouse_id or "the source warehouse"
    if re.search(r"negative stock not permitted|no inventory items found for external id", text, re.IGNORECASE):
        return {
            "error": f"Not enough stock in {warehouse_label} to move this quantity of {material_id}. Issue a lower quantity or check the warehouse balance in SAP.",
            "error_hi": f"{material_id} की इतनी मात्रा मूव करने के लिए {warehouse_label} में पर्याप्त स्टॉक नहीं है। कृपया कम मात्रा जारी करें या SAP में गोदाम का बैलेंस जांचें।",
        }
    if re.search(r"logistics area.*invalid|invalid.*logistics area", text, re.IGNORECASE):
        return {"error": f"SAP rejected this movement - the warehouse ID sent for {material_id} was invalid. Contact IT.", "error_hi": f"SAP ने यह मूवमेंट अस्वीकार कर दिया - {material_id} के लिए भेजा गया वेयरहाउस ID अमान्य था। कृपया IT से संपर्क करें।"}
    if re.search(r"authentication failed", text, re.IGNORECASE):
        return {"error": "SAP login failed while trying to move this stock. Contact IT.", "error_hi": "इस स्टॉक को मूव करने के दौरान SAP लॉगिन विफल हुआ। कृपया IT से संपर्क करें।"}
    if re.search(r"unreachable|timeout|connection", text, re.IGNORECASE):
        return {"error": "Could not reach SAP to move this stock. Please retry in a moment.", "error_hi": "इस स्टॉक को मूव करने के लिए SAP से संपर्क नहीं हो सका। कृपया थोड़ी देर बाद पुनः प्रयास करें।"}
    return {"error": f"SAP rejected this stock movement for {material_id}. Contact IT with the Request/Issue ID if this keeps happening.", "error_hi": f"SAP ने {material_id} के लिए यह स्टॉक मूवमेंट अस्वीकार कर दिया। यदि यह बार-बार हो रहा है तो कृपया Request/Issue ID के साथ IT से संपर्क करें।"}


def _trigger_goods_movement(sap_client, owner_party_id, product_id, source_warehouse, target_warehouse, quantity, uom, site_id) -> dict:
    import os
    import time
    dry_run = os.environ.get("SAP_GOODS_MOVEMENT_DRY_RUN", "true").lower() != "false"
    last_error = None
    for attempt in range(_GOODS_MOVEMENT_MAX_ATTEMPTS):
        try:
            result = sap_client.goods_movement(
                owner_party_id=owner_party_id, product_id=product_id,
                source_logistics_area_id=source_warehouse, target_logistics_area_id=target_warehouse,
                quantity=quantity, quantity_uom=uom, site_id=site_id, dry_run=dry_run,
            )
            sap_error = _has_sap_log_error(result)
            if sap_error and result.get("ok"):
                result = {**result, "ok": False, "error_detail": f"SAP rejected the movement: {sap_error}", "error": sap_error}
            return {**result, "attempted": True}
        except Exception as e:
            last_error = e
            is_auth_failure = "authentication failed" in str(e).lower()
            if is_auth_failure or attempt == _GOODS_MOVEMENT_MAX_ATTEMPTS - 1:
                break
            time.sleep(_GOODS_MOVEMENT_RETRY_DELAY_SECONDS)
    return {"ok": False, "error": str(last_error), "attempted": True}
```

### `inbound_receipt_service.py` (new file, full - THIS IS THE CORE FILE, fully rewritten for v2)

```python
"""Inbound STO Receipt - fully synchronous, zero polling/webhooks/background sweeps.

Two SEPARATE steps now (Sep 23 2026 redesign - a detail modal fires step 1 the instant it opens,
overlapping that fast call with the time the user spends reading the shipment preview; step 2
only fires once the user explicitly confirms):

  STEP 1 (start_automated_receipt) - Acknowledge -> PostGoodsReceipt directly (Path A). If
  rejected: Release -> immediate Warehouse Order lookup -> ConfirmAsPlanned (Path B fallback).
  Sets receipt_status="awaiting_relocation" when done - does NOT move any stock yet.

  STEP 2 (receive_stock_transfer_order / retry_receipt_relocation) - moves stock from
  {SITE}-HOLD to the STO's real destination warehouse. retry_receipt_relocation ONLY re-attempts
  lines that failed last time - already-successful lines' real SAP gac_id is never touched again
  (a real bug: retrying used to re-submit successful lines too, which either falsely turned a
  success into an error, or could double-move stock)."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import job_store
from sap_rate_limiter import SAP_MAX_CONCURRENT_REQUESTS
from sap_wip_clearing_client import company_and_set_of_books_for_site, inbound_staging_area_for_site
from store_approval_service import _trigger_goods_movement, _clarify_goods_movement_error

logger = logging.getLogger(__name__)

STO_COLLECTION = "stock_transfer_orders"
_PENDING_QUERY = {"gi_status": "posted", "receipt_status": {"$nin": ["received"]}}
_SAP_FINISHED_STATUS_CODE = "3"


def _receipt_hold_warehouse_id(site_id: str) -> str:
    return inbound_staging_area_for_site(site_id)


def _relocate_receipt_from_hold(db, sap_goods_movement_client, doc: dict, quantity_overrides: dict = None) -> dict:
    """STEP 2's actual work. Never raises - failure here must never undo an already-successful
    receipt; caller stores the result. Fires all lines concurrently (still capped at
    SAP_MAX_CONCURRENT_REQUESTS in-flight via sap_semaphore inside _trigger_goods_movement)."""
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
    with ThreadPoolExecutor(max_workers=SAP_MAX_CONCURRENT_REQUESTS) as pool:
        futures = [
            pool.submit(
                _trigger_goods_movement, sap_goods_movement_client, owner_party_id, item["product_id"],
                hold_warehouse_id, ship_to_location_id,
                quantity_overrides.get(str(item["line_no"]), item["requested_qty"]),
                item.get("unit_of_measure") or "EA", site_id,
            )
            for item in items
        ]
        line_results = []
        for item, future in zip(items, futures):
            moved_qty = quantity_overrides.get(str(item["line_no"]), item["requested_qty"])
            result = future.result()
            if not result.get("ok"):
                raw = result.get("error_detail") or result.get("error") or ""
                clarified = _clarify_goods_movement_error(raw, item["product_id"], hold_warehouse_id)
                line_results.append({"product_id": item["product_id"], "ok": False, "error": clarified["error"], "quantity": moved_qty, "unit_of_measure": item.get("unit_of_measure")})
            else:
                line_results.append({"product_id": item["product_id"], "ok": True, "gac_id": result.get("external_id"), "quantity": moved_qty, "unit_of_measure": item.get("unit_of_measure")})
    all_ok = all(r["ok"] for r in line_results)
    any_ok = any(r["ok"] for r in line_results)
    status = "done" if all_ok else ("partial" if any_ok else "failed")
    return {"status": status, "to": ship_to_location_id, "lines": line_results}


def retry_receipt_relocation(db, sap_goods_movement_client, sto_id: str) -> dict:
    """Retries STEP 2 - ONLY the lines that failed last time; already-successful lines' results
    are preserved untouched (see module docstring for why this matters)."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("receipt_status") not in ("received", "partial", "failed"):
        raise ValueError("This order must be received before retrying the warehouse move.")
    prior_lines = (doc.get("receipt_relocation") or {}).get("lines") or []
    already_ok = {l["product_id"]: l for l in prior_lines if l.get("ok")}
    retry_doc = doc
    if prior_lines:
        retry_doc = {**doc, "items": [it for it in (doc.get("items") or []) if it["product_id"] not in already_ok]}
    result = _relocate_receipt_from_hold(db, sap_goods_movement_client, retry_doc)
    merged_lines = list(already_ok.values()) + (result.get("lines") or [])
    all_ok = all(l["ok"] for l in merged_lines) if merged_lines else True
    any_ok = any(l["ok"] for l in merged_lines)
    merged_status = "done" if all_ok else ("partial" if any_ok else "failed")
    result = {**result, "lines": merged_lines, "status": merged_status}
    status_map = {"done": "received", "partial": "partial", "failed": "failed"}
    overall = status_map.get(result.get("status"), doc.get("receipt_status"))
    error_summary = " | ".join(f"{l['product_id']}: {l['error']}" for l in merged_lines if not l["ok"]) or None
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "receipt_relocation": result, "receipt_status": overall, "receipt_error": error_summary,
        "receipt_results": merged_lines or doc.get("receipt_results"),
    }})
    return result


def _enrich_relocation_lines(doc: dict) -> dict:
    """Backfills `quantity`/`unit_of_measure` for any line stored BEFORE that field existed
    (or preserved untouched across a retry) from the STO's own `items`, at READ time - fixes
    display for every record old or new without touching stored data."""
    relocation = doc.get("receipt_relocation")
    if not relocation or not relocation.get("lines"):
        return relocation
    by_product = {it["product_id"]: it for it in (doc.get("items") or [])}
    enriched_lines = []
    for line in relocation["lines"]:
        if line.get("quantity") is None:
            item = by_product.get(line["product_id"])
            if item:
                line = {**line, "quantity": item.get("requested_qty"), "unit_of_measure": item.get("unit_of_measure")}
        enriched_lines.append(line)
    return {**relocation, "lines": enriched_lines}


def list_ship_to_sites_with_pending_receipts(db) -> list:
    return sorted(s for s in db[STO_COLLECTION].distinct("ship_to_site_id", _PENDING_QUERY) if s)


def list_pending_receipts(db, site_id: str = None) -> list:
    """Simplified from the real app (which also live-verifies/backfills SAP delivery IDs here -
    port that from your own STO-creation tracking if needed). `active_job` surfaces a
    currently-running job of EITHER kind (receipt or relocation) so a page refresh/second tab
    still shows the live state."""
    query = dict(_PENDING_QUERY)
    if site_id:
        query["ship_to_site_id"] = site_id
    docs = list(db[STO_COLLECTION].find(query).sort("created_at", -1).limit(200))
    results = []
    for doc in docs:
        results.append({
            "sto_id": doc["_id"],
            "sap_order_id": (doc.get("sap_order_id") or "").lstrip("0") or doc.get("sap_order_id"),
            "ship_from_site_id": doc.get("ship_from_site_id"),
            "ship_to_site_id": doc.get("ship_to_site_id"),
            "ship_to_location_name": doc.get("ship_to_location_name"),
            "created_at": doc.get("created_at"),
            "receipt_status": doc.get("receipt_status") or "pending",
            "receipt_error": _humanize_sap_error(doc.get("receipt_error")),
            "receipt_relocation": _enrich_relocation_lines(doc),
            "outbound_delivery_ids": doc.get("outbound_delivery_ids") or [],
            "inbound_delivery_ids": doc.get("inbound_delivery_ids") or [],
            "items": [
                {"line_no": it.get("line_no"), "product_id": it.get("product_id"), "description": it.get("description"),
                 "unit_of_measure": it.get("unit_of_measure"), "requested_qty": it.get("requested_qty")}
                for it in (doc.get("items") or [])
            ],
        })
    active_jobs = {
        j["sto_id"]: {"job_id": j["_id"], "phase": j.get("phase")}
        for j in db[job_store.COLLECTION_NAME].find({"sto_id": {"$in": [r["sto_id"] for r in results]}, "kind": {"$in": ["inbound_receipt", "inbound_relocation"]}, "status": "running"})
    }
    for r in results:
        r["active_job"] = active_jobs.get(r["sto_id"])
    return results


def list_completed_receipts(db, site_id: str = None, date_from: datetime = None, date_to: datetime = None) -> list:
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
        "receipt_completed_at": doc.get("receipt_completed_at"),
        "receipt_duration_seconds": doc.get("receipt_duration_seconds"),
        "items": [
            {"line_no": it.get("line_no"), "product_id": it.get("product_id"), "description": it.get("description"),
             "unit_of_measure": it.get("unit_of_measure"), "requested_qty": it.get("requested_qty")}
            for it in (doc.get("items") or [])
        ],
        "receipt_relocation": _enrich_relocation_lines(doc),
        "outbound_delivery_ids": doc.get("outbound_delivery_ids") or [],
        "inbound_delivery_ids": doc.get("inbound_delivery_ids") or [],
    } for doc in docs]


def prepare_receipt(db, sto_id: str) -> dict:
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("gi_status") != "posted":
        raise ValueError("This order's Goods Issue hasn't posted in SAP yet - nothing to receive.")
    if not doc.get("outbound_delivery_ids"):
        raise ValueError("No SAP delivery reference found yet for this order - please retry in a moment.")
    if not doc.get("items"):
        raise ValueError(f"Stock Transfer Order {sto_id} has no line items - nothing to receive.")
    return doc


def receive_stock_transfer_order(db, sap_goods_movement_client, sto_id: str, actor: str, quantity_overrides: dict = None) -> dict:
    """STEP 2, first attempt (see retry_receipt_relocation for subsequent retries)."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id}) or {}
    if quantity_overrides is None:
        quantity_overrides = doc.get("receipt_quantity_overrides") or {}
    now = datetime.now(timezone.utc)
    started_at = doc.get("receipt_started_at")
    if started_at and started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    relocation = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc, quantity_overrides)
    status_map = {"done": "received", "partial": "partial", "failed": "failed",
                  "skipped_same_warehouse": "received", "skipped_no_items": "received"}
    overall = status_map.get(relocation.get("status"), "failed")
    lines = relocation.get("lines") or []
    error_summary = " | ".join(f"{l['product_id']}: {l['error']}" for l in lines if not l["ok"]) or None
    update = {
        "receipt_status": overall, "receipt_results": lines, "receipt_relocation": relocation,
        "receipt_error": error_summary, "received_at": now if overall in ("received", "partial") else doc.get("received_at"),
        "received_by": actor, "receipt_completed_at": now,
        "receipt_duration_seconds": round((now - started_at).total_seconds()) if started_at else None,
    }
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": update})
    return {"status": overall, "results": lines, "error": error_summary, "receipt_relocation": relocation}


def build_line_overrides(doc: dict, quantity_overrides: dict) -> dict:
    quantity_overrides = quantity_overrides or {}
    result = {}
    for it in (doc.get("items") or []):
        override = quantity_overrides.get(str(it["line_no"]))
        if override is not None and abs(float(override) - float(it["requested_qty"])) > 1e-6:
            result[it["product_id"]] = float(override)
    return result


def _find_matching_lot(sap_execution_client, site_id: str, product_ids: list) -> dict:
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


def _apply_quantity_overrides(sap_inbound_delivery_client, delivery_id: str, line_overrides: dict) -> None:
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


def start_automated_receipt(
    db, sap_kh_inbound_delivery_client, sap_inbound_delivery_client, sap_execution_client,
    sto_id: str, actor: str, quantity_overrides: dict = None,
) -> dict:
    """STEP 1 ONLY (Sep 23 2026 split) - THIS is what the modal fires the instant it opens.
    Does NOT move any stock - sets receipt_status="awaiting_relocation" when done, and the
    modal's "Confirm" then fires STEP 2 (receive_stock_transfer_order) separately."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    delivery_ids = doc.get("inbound_delivery_ids") or doc.get("outbound_delivery_ids") or []
    site_id = doc.get("ship_to_site_id")
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
            # PATH A - direct PostGoodsReceipt, NO Release call first (calling Release before this
            # disables PGRBackground for this tenant once already-released-without-GR).
            sap_inbound_delivery_client.post_goods_receipt(found["object_id"])
        except Exception as e:
            pgr_error = e
        if pgr_error is None:
            continue  # Path A succeeded
        # PATH B fallback - this delivery rejected direct PGR (already Released-but-unfinished).
        try:
            sap_kh_inbound_delivery_client.release_delivery(found["object_id"])
        except Exception as e:
            logger.info(f"Release for {delivery_id} ({sto_id}) skipped/failed (may already be released): {e}")
        refreshed = sap_kh_inbound_delivery_client.find_object_id(delivery_id) or found
        if refreshed.get("delivery_processing_status_code") == _SAP_FINISHED_STATUS_CODE:
            continue
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
    db_doc = db[STO_COLLECTION].find_one({"_id": sto_id}) or {}
    if not db_doc.get("receipt_relocation"):
        db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {"receipt_status": "awaiting_relocation", "receipt_error": None}})
    return {"status": "goods_receipt_posted"}


def _humanize_sap_error(raw: str) -> str:
    """Extracts just the message text from a raw SAP JSON fault; caps anything else at 300
    chars (cut at a word boundary, not mid-word) rather than dumping it verbatim."""
    if not raw:
        return raw
    match = re.search(r'"value"\s*:\s*"([^"]+)"', raw) or re.search(r'"message"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1)
    if len(raw) <= 300:
        return raw
    cut = raw[:300].rsplit(" ", 1)[0]
    return cut + "…"
```

## 3. `server.py` wiring — client instantiation (unchanged) + routes (UPDATED for v2, 6 routes now)

```python
import inbound_receipt_service
from sap_inbound_delivery_client import SAPInboundDeliveryClient
from sap_inbound_delivery_execution_client import SAPInboundDeliveryExecutionClient
from sap_goods_movement_client import SAPGoodsMovementClient

sap_inbound_delivery_client = SAPInboundDeliveryClient(
    endpoint=os.environ['SAP_ODATA_INBOUND_BASE_URL'], username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'], vhost=os.environ['BYD_ODATA_VHOST'],
)
sap_kh_inbound_delivery_client = SAPInboundDeliveryClient(
    endpoint=os.environ['SAP_ODATA_KH_INBOUND_DELIVERY_BASE_URL'], username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'], vhost=os.environ['BYD_ODATA_VHOST'], release_action="Release",
)
sap_inbound_delivery_execution_client = SAPInboundDeliveryExecutionClient(
    endpoint=os.environ['SAP_ODATA_INBOUND_DELIVERY_EXECUTION_BASE_URL'], username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'], vhost=os.environ['BYD_ODATA_VHOST'],
)
sap_goods_movement_client = SAPGoodsMovementClient(
    endpoint=os.environ['SAP_SOAP_GOODS_MOVEMENT_ENDPOINT'], username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)


# ==================== Inbound STO Receipt routes (v2) ====================

class InboundReceiptItem(BaseModel):
    line_no: int
    received_qty: float


class InboundReceiptRequest(BaseModel):
    items: List[InboundReceiptItem] = []


@api_router.get("/inbound-receipts/sites")
async def get_inbound_receipt_sites():
    return {"sites": await asyncio.to_thread(inbound_receipt_service.list_ship_to_sites_with_pending_receipts, db)}


@api_router.get("/inbound-receipts/pending")
async def get_inbound_receipts_pending(site_id: Optional[str] = None):
    return {"orders": await asyncio.to_thread(inbound_receipt_service.list_pending_receipts, db, site_id)}


@api_router.get("/inbound-receipts/completed")
async def get_inbound_receipts_completed(site_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None):
    try:
        parsed_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc) if date_from else None
        parsed_to = (datetime.fromisoformat(date_to) + timedelta(days=1)).replace(tzinfo=timezone.utc) if date_to else None
    except ValueError:
        raise HTTPException(status_code=400, detail="date_from/date_to must be YYYY-MM-DD")
    return {"orders": await asyncio.to_thread(inbound_receipt_service.list_completed_receipts, db, site_id, parsed_from, parsed_to)}


# STEP 1 - fired by the modal the instant it opens (mode="receive").
@api_router.post("/inbound-receipts/{sto_id}/receive")
async def post_inbound_receipt(sto_id: str, payload: InboundReceiptRequest, request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)  # replace with your own auth
    actor = (user or {}).get("name") or (user or {}).get("email") or "unknown"
    overrides = {str(i.line_no): i.received_qty for i in payload.items}
    try:
        doc = await asyncio.to_thread(inbound_receipt_service.prepare_receipt, db, sto_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if doc.get("receipt_status") == "received":
        return {"already_received": True, "result": {"status": "received", "results": doc.get("receipt_results") or []}}

    existing_job = await asyncio.to_thread(db[job_store.COLLECTION_NAME].find_one, {"sto_id": sto_id, "status": "running", "kind": "inbound_receipt"})
    if existing_job:
        return {"job_id": existing_job["_id"]}

    job_id = str(uuid.uuid4())
    await asyncio.to_thread(db[inbound_receipt_service.STO_COLLECTION].update_one, {"_id": sto_id}, {"$set": {"receipt_started_at": datetime.now(timezone.utc)}})
    await asyncio.to_thread(job_store.create_job, db, job_id, {
        "sto_id": sto_id, "kind": "inbound_receipt", "status": "running", "phase": "processing", "result": None, "error": None,
    })

    async def run():
        try:
            final = await asyncio.to_thread(
                inbound_receipt_service.start_automated_receipt, db,
                sap_kh_inbound_delivery_client, sap_inbound_delivery_client, sap_inbound_delivery_execution_client,
                sto_id, actor, overrides,
            )
            await asyncio.to_thread(job_store.update_job, db, job_id, {"status": "done", "phase": "done", "result": final, "error": None})
        except Exception as e:
            await asyncio.to_thread(job_store.update_job, db, job_id, {"status": "failed", "phase": "failed", "result": None, "error": str(e)})
            await asyncio.to_thread(db[inbound_receipt_service.STO_COLLECTION].update_one, {"_id": sto_id}, {"$set": {"receipt_status": "failed", "receipt_error": str(e)}})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/inbound-receipts/receive-status/{job_id}")
async def get_inbound_receipt_job_status(job_id: str):
    job = await asyncio.to_thread(job_store.get_job, db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


# STEP 2 (NEW route, v2) - fired by the modal's "Confirm" button, or its "Retry Warehouse Move"
# button. Branches to a first-time move or a failed-lines-only retry.
@api_router.post("/inbound-receipts/{sto_id}/relocate")
async def post_inbound_receipt_relocate(sto_id: str, request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    actor = (user or {}).get("name") or (user or {}).get("email") or "unknown"
    doc = await asyncio.to_thread(db[inbound_receipt_service.STO_COLLECTION].find_one, {"_id": sto_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Stock Transfer Order {sto_id} not found.")
    existing_job = await asyncio.to_thread(db[job_store.COLLECTION_NAME].find_one, {"sto_id": sto_id, "status": "running", "kind": "inbound_relocation"})
    if existing_job:
        return {"job_id": existing_job["_id"]}
    has_prior_attempt = bool(doc.get("receipt_relocation"))
    job_id = str(uuid.uuid4())
    await asyncio.to_thread(job_store.create_job, db, job_id, {
        "sto_id": sto_id, "kind": "inbound_relocation", "status": "running", "phase": "moving", "result": None, "error": None,
    })

    async def run():
        try:
            if has_prior_attempt:
                result = await asyncio.to_thread(inbound_receipt_service.retry_receipt_relocation, db, sap_goods_movement_client, sto_id)
            else:
                result = await asyncio.to_thread(inbound_receipt_service.receive_stock_transfer_order, db, sap_goods_movement_client, sto_id, actor, None)
            await asyncio.to_thread(job_store.update_job, db, job_id, {"status": "done", "phase": "done", "result": result, "error": None})
        except Exception as e:
            await asyncio.to_thread(job_store.update_job, db, job_id, {"status": "failed", "phase": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}
```

## 4. MongoDB doc shape on `stock_transfer_orders` (updated `receipt_status` values)

`receipt_status` now has 5 possible values: `"pending"` -> `"awaiting_relocation"` (Step 1 done,
Step 2 not yet) -> `"received"` / `"partial"` / `"failed"` (Step 2's outcome). A "partial"/"failed"
row is still queryable/actionable in `list_pending_receipts` (not `"received"`) - a
`"partial"/"failed"` row ALSO shows in `list_completed_receipts`. `outbound_delivery_ids`/
`inbound_delivery_ids` (SAP delivery numbers, e.g. `"P1D1-541"`) must be populated on the doc for
the modal to show them.

```json
{
  "_id": "STO-000142", "sap_order_id": "0000032871",
  "ship_from_site_id": "P9", "ship_to_site_id": "P2", "ship_to_location_id": "P2-RM",
  "ship_to_location_name": "P2 Raw Material", "gi_status": "posted",
  "outbound_delivery_ids": ["P9D1-431"], "inbound_delivery_ids": ["P9D1-431"],
  "items": [{"line_no": 1, "product_id": "G12NUT", "description": "...", "unit_of_measure": "EA", "requested_qty": 100}],
  "receipt_status": "pending", "created_at": "2026-09-22T10:00:00Z"
}
```

## 5. Frontend — 2 files: `ReceiptDetailModal.jsx` (NEW) + `InboundReceiptsPage.js` (simplified)

Copy both files EXACTLY as they exist right now in this app:
- `/app/frontend/src/components/ReceiptDetailModal.jsx` - the shared modal (receive/view modes,
  fires Step 1 on open, Step 2 on Confirm, staged progress bar, per-line result with quantity
  moved, Retry buttons for either step's failure).
- `/app/frontend/src/pages/InboundReceiptsPage.js` - Pending/Completed tables (NO bulk selection,
  NO per-row popover/expand - "Receive"/"View" buttons just open the modal).

Both use `axios`, `@phosphor-icons/react`, and shadcn/ui (`button`, `input`, `progress`, `dialog`,
`table`, `select`, `sonner`). Swap `NavTabs`/`SapConnectionStatus`/`ErpConnectionStatus`/
`useAuth` for your own app's equivalents (or remove them).

Add the route:
```jsx
<Route path="/inventory/inbound-receipts" element={<InboundReceiptsPage />} />
```

## 6. Checklist to wire this into the new app
1. Copy the backend files (section 2) - note `store_approval_service`'s extract now includes
   `_clarify_goods_movement_error`, not just the retry wrapper.
2. Add the `.env` keys (section 1).
3. Add `job_store.ensure_indexes(db)` to your startup block (once).
4. Wire client instantiations + ALL 6 routes into `server.py` (section 3) - note `/receive` no
   longer does the warehouse move, and `/relocate` is a NEW route.
5. Make sure your STO-creation flow populates `gi_status`, `outbound_delivery_ids`,
   `inbound_delivery_ids`, `ship_to_site_id`, `ship_to_location_id`,
   `items[].product_id/unit_of_measure/requested_qty` (section 4).
6. Copy both frontend files (section 5), add the route.
7. Set `SAP_GOODS_MOVEMENT_DRY_RUN="false"` only once you've reviewed a batch of dry runs.
8. Test with one real never-touched STO end-to-end - watch for the modal firing Step 1 fast, then
   Step 2's progress bar, then the final per-line result with quantity + gac_id.

## 7. Known rough edges (not yet fixed here, low priority)
- Partial STOs show in BOTH Pending and Completed tabs (both queries include "partial").
- No Retry-from-view for a Step-1-only failure (user must use the Pending tab's "Receive" again).
- If your dev environment shows an ENOSPC file-watcher crash-loop, add `CHOKIDAR_USEPOLLING=true`
  to `frontend/.env` (fixes the `public/` folder watcher; webpack's own `src/` watcher may still
  log non-fatal warnings - do a manual frontend restart if a change doesn't seem to apply).
