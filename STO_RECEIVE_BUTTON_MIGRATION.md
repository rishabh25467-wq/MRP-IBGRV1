# STO "Receive" Button — Full Migration Bundle (v3, Sep 24 2026 — atomic relocation)

Everything needed to port the Inbound STO Receipt flow into a new app. This version REPLACES v2
(Sep 23 2026 redesign doc) - the warehouse-move step (`{SITE}-HOLD` -> real destination) changed
from "N independent parallel SOAP calls, one per line, so one bad line never blocks the rest" to
"ONE atomic SOAP call covering every line under ONE Goods Movement ID - either every line moves,
or NONE of them move" (explicit user mandate: "we do not intend to have one fail, one pass. All
must pass at once or all fail. nothing moves."). The PGR (Post Goods Receipt) step is UNCHANGED -
it uses a completely different SAP service (an OData Function Import, one call per delivery
object) and was confirmed NOT batchable the same way (see section 0).

```
Acknowledge -> PostGoodsReceipt (Path A, primary, per delivery - unchanged, NOT atomic-batchable)
   -> if rejected: Release -> immediate Warehouse-Order lookup -> ConfirmAsPlanned (Path B, fallback)
[receipt_status = "awaiting_relocation" here - this is the modal's "Confirm" checkpoint]
-> Goods Movement: {SITE}-HOLD -> real target warehouse, ONE atomic SOAP call, ONE Goods
   Movement ID for every line - all lines succeed together or SAP rejects the whole call and
   NOTHING moves (retry always resubmits every line again, never a partial subset)
```

## 0. Why PGR could NOT get the same atomic treatment (read this before assuming it can)

Two completely different SAP services are involved and they behave differently:

- **Goods Movement / relocation** (`InventoryProcessingGoodsAndActivityConfirmationGoodsMovementIn.
  DoGoodsMovement`, SOAP) - its schema allows ONE `<GoodsAndActivityConfirmation>` (ONE
  `ExternalID`/GACID) to contain MULTIPLE `<InventoryChangeItemGoodsMovement>` blocks (one per
  line). Live-tested against production: bundling 2 lines (1 valid + 1 deliberately invalid,
  "negative stock") in one call made SAP reject the WHOLE call as a SOAP Fault (HTTP 500) -
  the valid line did NOT post either. Zero partial/leaked stock movement, confirmed via a
  follow-up inventory read. This is the primitive the atomic redesign below is built on.
- **Post Goods Receipt** (`InboundDeliveryPGRBackground`, an OData **Function Import**) - takes
  exactly ONE `ObjectID` (one Inbound Delivery Notification) per call and already posts every
  item ON THAT delivery in one shot. But this tenant's Goods Issue always creates ONE Outbound
  Delivery -> ONE Inbound Delivery Notification PER STO LINE (confirmed live), so a 3-line STO
  is 3 separate delivery objects needing 3 separate `InboundDeliveryPGRBackground` calls - a
  Function Import has no multi-block envelope trick like the SOAP service above. OData `$batch`
  could wrap multiple Function Import calls in one HTTP request, but each one would still
  execute/commit independently inside the batch (no transactional guarantee across them) - this
  exact combination was never tested and is NOT assumed to work. **Conclusion: PGR stays
  per-delivery, looped, as it always was; only the relocation step became atomic.**

## 1. Required `.env` variables (backend) — unchanged from v1/v2

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

## 2. Backend files — unchanged from v1/v2, copy verbatim if you don't have them yet
`sap_rate_limiter.py`, `sap_wip_clearing_client.py`, `sap_inbound_delivery_client.py`,
`sap_inbound_delivery_execution_client.py`, `job_store.py`. Also still need
`store_approval_service.is_dry_run` and `_clarify_goods_movement_error` (used ONLY for the
single-item `goods_movement` flow used elsewhere in the app, e.g. Store Approval - the new
`goods_movement_batch` method below does NOT touch that flow).

### `sap_goods_movement_client.py` — UPDATED (adds the atomic `goods_movement_batch` method)
The single-item `goods_movement()` method and its `_build_envelope`/`_extract_sap_error`/
`_extract_gac_id`/`_normalize_logistics_area_id` helpers are UNCHANGED from v2 - keep them (other
flows in the app, e.g. Store Approval's stock issue, still use single-item `goods_movement`).
ADD everything below to the same file:

```python
def _build_item_block(external_item_id, product_id, owner_party_id, source_area, target_area,
                       quantity, unit_code, quantity_type_code,
                       target_stock_status_code="", target_restricted_use=False) -> str:
    from xml.sax.saxutils import escape
    product_id, owner_party_id = escape(product_id), escape(owner_party_id)
    source_area, target_area = escape(source_area), escape(target_area)
    quantity_str = format(quantity, "f").rstrip("0").rstrip(".") or "0"
    restricted_str = "true" if target_restricted_use else "false"
    return f"""        <InventoryChangeItemGoodsMovement>
          <ExternalItemID>{external_item_id}</ExternalItemID>
          <MaterialInternalID>{product_id}</MaterialInternalID>
          <OwnerPartyInternalID>{owner_party_id}</OwnerPartyInternalID>
          <InventoryRestrictedUseIndicator>{restricted_str}</InventoryRestrictedUseIndicator>
          <InventoryStockStatusCode>{target_stock_status_code}</InventoryStockStatusCode>
          <SourceLogisticsAreaID>{source_area}</SourceLogisticsAreaID>
          <TargetLogisticsAreaID>{target_area}</TargetLogisticsAreaID>
          <InventoryItemChangeQuantity>
            <Quantity unitCode="{unit_code}">{quantity_str}</Quantity>
            <QuantityTypeCode>{quantity_type_code}</QuantityTypeCode>
          </InventoryItemChangeQuantity>
          <SourceInventoryRestrictedUseIndicator>false</SourceInventoryRestrictedUseIndicator>
        </InventoryChangeItemGoodsMovement>"""


# ONE <GoodsAndActivityConfirmation> header (ONE ExternalID/GACID) wrapping every line's own
# <InventoryChangeItemGoodsMovement> block - SAP processes one SOAP call as ONE all-or-nothing
# BOI transaction regardless of item-block count (live-proven: an invalid line makes SAP reject
# the WHOLE call as a SOAP Fault/HTTP 500, zero partial posting). No documented hard cap on item
# count (SAP Help: PSM_ISI_R_II_APGACFM_GOODS_MOVEMENT_IN) - only the usual synchronous
# payload-size/timeout ceiling, so a very large STO could theoretically time out.
def _build_batch_envelope(external_id, site_id, transaction_dt, item_blocks: list) -> str:
    from xml.sax.saxutils import escape
    site_id = escape(site_id)
    items_xml = "\n".join(item_blocks)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
  <soapenv:Body>
    <glob:GoodsAndActivityConfirmationGoodsMovement>
      <GoodsAndActivityConfirmation>
        <ExternalID>{external_id}</ExternalID>
        <SiteID>{site_id}</SiteID>
        <TransactionDateTime>{transaction_dt}</TransactionDateTime>
{items_xml}
      </GoodsAndActivityConfirmation>
    </glob:GoodsAndActivityConfirmationGoodsMovement>
  </soapenv:Body>
</soapenv:Envelope>"""


def _extract_soap_fault(xml: str):
    """A rejected multi-item confirmation comes back as an actual SOAP Fault (HTTP 500), not the
    HTTP-200-with-<SeverityCode> shape `_extract_sap_error` handles. Real example:
    `<faultText>Negative stock not permitted in logistics area P1-HOLD, material
    09200726-01</faultText>`. Prefers <faultText> (human-readable) over <faultstring>
    ("Application exception occurred!")."""
    text = re.search(r"<faultText>(.*?)</faultText>", xml, re.DOTALL)
    if text and text.group(1).strip():
        return text.group(1).strip()
    fault = re.search(r"<faultstring>(.*?)</faultstring>", xml, re.DOTALL)
    return fault.group(1).strip() if fault else None


def _attribute_fault_to_line(fault_text: str, lines: list):
    """SAP's fault text usually names the exact material ID it rejected - match it back to one
    of the lines we sent so the UI can point at the real culprit instead of blaming every line
    equally. Returns None (not every fault names a material) rather than guessing."""
    if not fault_text:
        return None
    for line in lines:
        if line["product_id"] and line["product_id"] in fault_text:
            return line["product_id"]
    return None
```

Add this method inside `SAPGoodsMovementClient`:

```python
    def goods_movement_batch(self, owner_party_id: str, site_id: str, lines: list, dry_run: bool = True,
                              target_stock_status_code: str = "", target_restricted_use: bool = False) -> dict:
        """Atomic multi-line version of `goods_movement`. `lines`: [{product_id,
        source_logistics_area_id, target_logistics_area_id, quantity, quantity_uom}, ...].
        ONE SOAP call, ONE ExternalID/GACID for every line - either every line posts or SAP
        rejects the whole call and nothing moves. Never returns a per-line ok/fail split - that
        is the whole point of "atomic"."""
        if not lines:
            raise SAPGoodsMovementError("no lines to move")
        for line in lines:
            if line["quantity"] <= 0:
                raise SAPGoodsMovementError(f"quantity must be > 0 for {line['product_id']}")
        external_id = f"MOV-{uuid.uuid4().hex[:6].upper()}"
        transaction_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        item_blocks = [
            _build_item_block(
                f"I-{uuid.uuid4().hex[:8].upper()}", line["product_id"], owner_party_id,
                _normalize_logistics_area_id(line["source_logistics_area_id"]),
                _normalize_logistics_area_id(line["target_logistics_area_id"]),
                line["quantity"], _resolve_unit_code(line["quantity_uom"]), line["quantity_uom"],
                target_stock_status_code=target_stock_status_code, target_restricted_use=target_restricted_use,
            )
            for line in lines
        ]
        envelope = _build_batch_envelope(external_id, site_id, transaction_dt, item_blocks)
        if dry_run:
            return {"ok": True, "dry_run": True, "external_id": external_id, "envelope": envelope}

        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                response = self.session.post(self.endpoint, data=envelope.encode("utf-8"), headers=headers, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SAPGoodsMovementError(f"SAP Goods Movement service unreachable: {e}")

        if response.status_code == 401:
            raise SAPGoodsMovementError("SAP SOAP authentication failed for Goods Movement (check SAP_SOAP_USERNAME/PASSWORD).")
        # SAP rejects a bad multi-item confirmation as a SOAP Fault (HTTP 500), not a
        # 200-with-log-error - see _extract_soap_fault docstring.
        if response.status_code == 500:
            fault = _extract_soap_fault(response.text) or "SAP rejected this batch of movements."
            return {"ok": False, "external_id": external_id, "error": fault,
                     "culprit_product_id": _attribute_fault_to_line(fault, lines), "raw_xml": response.text}
        if response.status_code != 200:
            raise SAPGoodsMovementError(f"SAP Goods Movement service responded with HTTP {response.status_code}: {response.text[:500]}")

        sap_error = _extract_sap_error(response.text)
        if sap_error:
            return {"ok": False, "external_id": external_id, "error": sap_error,
                     "culprit_product_id": _attribute_fault_to_line(sap_error, lines), "raw_xml": response.text}
        return {"ok": True, "external_id": _extract_gac_id(response.text) or external_id, "client_reference_id": external_id, "raw_xml": response.text}
```

### `store_approval_service.py` extract — needed for `is_dry_run` + the error clarifier
```python
import os
import re

def is_dry_run() -> bool:
    """Read LAZILY (a function, not a module constant) - a module-level os.environ.get() at
    import time can run BEFORE load_dotenv(), silently staying dry-run forever."""
    value = os.environ.get("SAP_GOODS_MOVEMENT_DRY_RUN", "true")
    return value.lower() != "false"


def _clarify_goods_movement_error(raw_error: str, material_id: str, warehouse_id: str = None) -> dict:
    """Translates SAP's raw Goods Movement rejection text into a plain-English message a
    warehouse user can act on - names the ACTUAL warehouse ID when known."""
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
```

### `inbound_receipt_service.py` (THE CORE FILE - relocation section fully rewritten for v3)

Everything from v2 stays the same EXCEPT `_relocate_receipt_from_hold` and
`retry_receipt_relocation`, which are replaced entirely (no more `ThreadPoolExecutor`, no more
per-line parallel calls, no more partial-merge-on-retry logic):

```python
import time
from sap_goods_movement_client import SAPGoodsMovementError
from store_approval_service import _clarify_goods_movement_error, is_dry_run

_RELOCATION_BATCH_MAX_ATTEMPTS = 3
_RELOCATION_BATCH_RETRY_DELAY_SECONDS = 5


def _trigger_goods_movement_batch(sap_client, owner_party_id, site_id, lines: list) -> dict:
    """Retry wrapper around `goods_movement_batch` - 3 attempts, 5s backoff, bail immediately on
    an auth failure (transport/HTTP-level errors only - a real SAP business rejection, e.g.
    negative stock, is a semantic decision and is NEVER retried, only transient/connection
    errors are). Never raises; caller always gets a dict back."""
    last_error = None
    for attempt in range(_RELOCATION_BATCH_MAX_ATTEMPTS):
        try:
            result = sap_client.goods_movement_batch(owner_party_id, site_id, lines, dry_run=is_dry_run())
            return {**result, "attempted": True}
        except SAPGoodsMovementError as e:
            last_error = e
            is_auth_failure = "authentication failed" in str(e).lower()
            if is_auth_failure or attempt == _RELOCATION_BATCH_MAX_ATTEMPTS - 1:
                break
            time.sleep(_RELOCATION_BATCH_RETRY_DELAY_SECONDS)
    return {"attempted": True, "ok": False, "error": str(last_error)}


def _relocate_receipt_from_hold(db, sap_goods_movement_client, doc: dict, quantity_overrides: dict = None) -> dict:
    """Moves every line's stock from {SITE}-HOLD to the STO's real destination warehouse in ONE
    atomic SOAP call - either all lines move (status "done", every line shares the SAME gac_id)
    or none do (status "failed", every line marked not-ok). Never raises - failure here must
    never undo an already-successful Goods Receipt; caller stores the result."""
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
    line_specs = [
        {
            "product_id": item["product_id"],
            "quantity": quantity_overrides.get(str(item["line_no"]), item["requested_qty"]),
            "quantity_uom": item.get("unit_of_measure") or "EA",
            "source_logistics_area_id": hold_warehouse_id,
            "target_logistics_area_id": ship_to_location_id,
        }
        for item in items
    ]
    result = _trigger_goods_movement_batch(sap_goods_movement_client, owner_party_id, site_id, line_specs)
    if result.get("ok"):
        line_results = [
            {"product_id": spec["product_id"], "ok": True, "gac_id": result.get("external_id"),
             "quantity": spec["quantity"], "unit_of_measure": spec["quantity_uom"]}
            for spec in line_specs
        ]
        return {"status": "done", "to": ship_to_location_id, "lines": line_results, "gac_id": result.get("external_id")}
    # Whole batch was rejected - every line is "not ok". Only the actual culprit (if SAP's fault
    # text named a real material) gets the true clarified SAP reason; every other line gets a
    # short note explaining it was blocked purely because it was bundled with the failing line.
    culprit_product_id = result.get("culprit_product_id")
    raw = result.get("error") or "SAP rejected this batch of movements."
    clarified = _clarify_goods_movement_error(raw, culprit_product_id or "this batch", hold_warehouse_id)
    line_results = []
    for spec in line_specs:
        is_culprit = culprit_product_id == spec["product_id"]
        error = clarified["error"] if (is_culprit or not culprit_product_id) else \
            f"Not moved - blocked because {culprit_product_id} in the same order failed: {clarified['error']}"
        line_results.append({
            "product_id": spec["product_id"], "ok": False, "error": error,
            "quantity": spec["quantity"], "unit_of_measure": spec["quantity_uom"],
        })
    return {"status": "failed", "to": ship_to_location_id, "lines": line_results}


def retry_receipt_relocation(db, sap_goods_movement_client, sto_id: str) -> dict:
    """Retry button on the Completed tab for when relocation failed. A failed attempt means
    NOTHING moved, so "retry" always resubmits every line together again, exactly like the
    first attempt - no more preserving/merging already-succeeded lines from a prior attempt
    (there is no such thing as a "prior partial success" with atomic all-or-nothing)."""
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("receipt_status") not in ("received", "partial", "failed"):
        raise ValueError("This order must be received before retrying the warehouse move.")
    result = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc)
    lines = result.get("lines") or []
    status_map = {"done": "received", "failed": "failed"}
    overall = status_map.get(result.get("status"), doc.get("receipt_status"))
    error_summary = " | ".join(f"{l['product_id']}: {l['error']}" for l in lines if not l["ok"]) or None
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "receipt_relocation": result, "receipt_status": overall, "receipt_error": error_summary,
        "receipt_results": lines or doc.get("receipt_results"),
    }})
    return result
```

`receive_stock_transfer_order` (first-time move, calls `_relocate_receipt_from_hold` the same way
as before) only needs its `status_map` trimmed - the relocation step can no longer produce
`"partial"`, only `"done"`/`"failed"`/`"skipped_*"`:
```python
    status_map = {
        "done": "received", "failed": "failed",
        "skipped_same_warehouse": "received", "skipped_no_items": "received",
    }
```
Everything else in `inbound_receipt_service.py` (STEP 1's `start_automated_receipt`,
`list_pending_receipts`, `list_completed_receipts`, `_enrich_relocation_lines`,
`prepare_receipt`, `build_line_overrides`, `_humanize_sap_error`) is UNCHANGED from v2 - copy
verbatim.

## 3. `server.py` wiring — UNCHANGED from v2 (same 6 routes, same client instantiation)
No route signatures changed - `/receive`, `/relocate`, `/retry-receipt-relocation`,
`/receive-status/{job_id}`, `/pending`, `/completed` all keep calling the exact same
`inbound_receipt_service` function names as before; only what happens INSIDE
`_relocate_receipt_from_hold`/`retry_receipt_relocation` changed. See v2's section 3 for the
full route code if you don't have it yet - copy verbatim, nothing to update here.

## 4. MongoDB doc shape on `stock_transfer_orders`

`receipt_status`: `"pending"` -> `"awaiting_relocation"` (Step 1/PGR done, Step 2/relocation not
yet) -> `"received"` / `"failed"` (Step 2's outcome - `"partial"` can no longer be PRODUCED by
new relocations, only ever seen on STOs relocated before this v3 change; keep it in any status
filter/query for backward compatibility with historical data, just don't expect new rows to get
it). `receipt_relocation` shape after this change:
```json
{
  "status": "done",
  "to": "P8-SFG",
  "gac_id": "283220",
  "lines": [
    {"product_id": "G12LW", "ok": true, "gac_id": "283220", "quantity": 1.0, "unit_of_measure": "EA"},
    {"product_id": "G12NUT", "ok": true, "gac_id": "283220", "quantity": 2.0, "unit_of_measure": "EA"}
  ]
}
```
Note EVERY line shares the SAME `gac_id` now (one Goods Movement ID per STO, not one per line).
On failure, every line has `"ok": false` - the culprit line's `error` is the real clarified SAP
reason, every other line's `error` says it was blocked because it was bundled with the culprit.

`outbound_delivery_ids`/`inbound_delivery_ids` (SAP delivery numbers, e.g. `"P1D1-541"`) must
still be populated on the doc for the modal to show them - unchanged from v2.

## 5. Frontend — 2 files, `ReceiptDetailModal.jsx` (UPDATED) + `InboundReceiptsPage.js` (unchanged)

Copy both files EXACTLY as they exist right now in this app:
- `/app/frontend/src/components/ReceiptDetailModal.jsx` - UPDATED twice this session:
  1. The poll handler that lands the relocation job's result now does
     `setFinalResult(data.result?.receipt_relocation || data.result)` instead of
     `setFinalResult(data.result)`. **Why this matters**: the first-time completion path
     (`receive_stock_transfer_order`) returns a WRAPPED object (`{status: "received"/"failed",
     results, error, receipt_relocation: {status: "done"/"failed", to, lines, gac_id}}`) where
     the overall `status` uses receipt-terminology ("received"), not relocation-terminology
     ("done") - while a retry (`retry_receipt_relocation`) returns the inner relocation shape
     directly. Reading `data.result` raw only worked for retries; it silently showed "Received"
     with no GM ID on a genuinely-fresh completion because `status !== "done"` never matched.
     If you see the same symptom (no GM ID / wrong banner text right after a fresh Confirm &
     Receive, but the Completed tab's OWN "view" looks fine) - this is the exact bug, check this
     line first.
  2. The "done" status banner now also shows the shared Goods Movement ID inline (`· GM
     {finalResult.gac_id}`) instead of only showing it per-line in the table below - since every
     line now shares ONE id, showing it once at the top is clearer.
- `/app/frontend/src/pages/InboundReceiptsPage.js` - unchanged from v2 (Pending/Completed
  tables, no bulk selection, "Receive"/"View" buttons open the modal).

Both use `axios`, `@phosphor-icons/react`, and shadcn/ui (`button`, `progress`, `dialog`,
`table`, `sonner`). Swap `NavTabs`/`useAuth`/connection-status widgets for your own app's
equivalents (or remove them).

Add the route:
```jsx
<Route path="/inventory/inbound-receipts" element={<InboundReceiptsPage />} />
```

## 6. Checklist to wire this into the new app
1. Copy the backend files (section 2) - `sap_goods_movement_client.py` needs BOTH the old
   single-item `goods_movement` (still used elsewhere, e.g. Store Approval) AND the new
   `goods_movement_batch` (used only by inbound receipt relocation).
2. Add the `.env` keys (section 1).
3. Add `job_store.ensure_indexes(db)` to your startup block (once).
4. Wire client instantiations + all 6 routes into `server.py` (unchanged from v2, see that
   doc's section 3 if you need the full route code).
5. Make sure your STO-creation flow populates `gi_status`, `outbound_delivery_ids`,
   `inbound_delivery_ids`, `ship_to_site_id`, `ship_to_location_id`,
   `items[].product_id/unit_of_measure/requested_qty` (section 4).
6. Copy both frontend files (section 5, note the 2 `ReceiptDetailModal.jsx` fixes above), add
   the route.
7. Set `SAP_GOODS_MOVEMENT_DRY_RUN="false"` only once you've reviewed a batch of dry runs -
   dry-run mode returns the built envelope without ever calling SAP, safe to inspect first.
8. Test with one real never-touched multi-line STO end-to-end - confirm ALL lines land the SAME
   `gac_id` on success, and that a deliberately-bad line (e.g. request more than what's in HOLD)
   blocks EVERY line in that STO, not just the bad one.

## 7. Known rough edges (not yet fixed here, low priority)
- Partial STOs (historical, pre-v3) show in BOTH Pending and Completed tabs (both queries
  include `"partial"` for backward compatibility).
- No Retry-from-view for a Step-1-only (PGR) failure (user must use the Pending tab's "Receive"
  again) - PGR itself was never made atomic (see section 0), so this isn't expected to change.
- A very large STO (15+ lines) bundled into one atomic call is UNTESTED at that size - only
  proven live with 2 lines. No documented SAP hard cap on item count, but the usual
  synchronous-call payload/timeout ceiling still applies; if you hit timeouts on very large
  orders, this is the first place to look.
- If your dev environment shows an ENOSPC file-watcher crash-loop, add `CHOKIDAR_USEPOLLING=true`
  to `frontend/.env` (fixes the `public/` folder watcher; webpack's own `src/` watcher may still
  log non-fatal warnings - do a manual frontend restart if a change doesn't seem to apply).
