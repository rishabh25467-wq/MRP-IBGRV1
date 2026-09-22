# STO Inbound Receipt: Architecture Pivot + Performance Investigation (Sep 22 2026)

Full detailed record of the same-day investigation thread. PRD.md Part 6/7/8 have the short
version - this is the complete trail with raw evidence, for any future agent who needs to
re-verify or extend this without re-doing the live testing.

## 1. Why this thread started
User rejected the previous session's webhook + 20-min safety-sweep architecture for STO Inbound
Goods Receipt outright: SAP's own async Warehouse Order creation lag (2-8 min) was being masked
by polling instead of surfaced. Mandate: a strictly synchronous, transaction-driven chain - zero
async waiting, zero polling, zero background jobs/webhooks.

## 2. First rebuild (superseded within the same day - see #3)
Removed: `/api/webhooks/sap-put-away`, `_verify_sap_webhook_auth`, `SAP_WEBHOOK_EVENTS_COLLECTION`,
`EXTERNAL_WEBHOOK_PATH_PREFIXES` bypass in `auth_service.py`, the 20-min
`start_awaiting_sap_receipt_timeout_loop` sweep in `server.py`, `complete_automated_receipt_for_lot`.
First version of `inbound_receipt_service.start_automated_receipt`: `Acknowledge -> Release ->
check Finished -> single immediate SiteLogisticsLot lookup (find_recent_lots, new method on
sap_inbound_delivery_execution_client.py) -> ConfirmAsPlanned -> fallback direct PostGoodsReceipt`.
Bug fixed along the way: `find_recent_lots`'s `$orderby=SystemAdministrativeData/CreationDateTime`
isn't exposed by the custom `khinbounddeliveryexecution` OData service - dropped `$orderby`
entirely, rely on `$top=50` + SAP's default order.
Live-tested on STO-000111 (P1) - succeeded, but only because the delivery was ALREADY Released
from earlier sessions (hit the "already Finished" shortcut, never actually exercised the Lot
lookup). STO-000123 (P1D1-560) failed fast - `Release`/`PostGoodsReceipt` both "action is
disabled" - at the time this was read as a genuine already-broken delivery (KBA 3583076), later
shown to be a symptom of the SAME root cause found in #3.

## 3. User's challenge - the fix in #2 never actually proved anything
User pointed out: every test up to this point called `Release` before `PostGoodsReceipt`, so
"action is disabled" never proved PGRBackground itself was blocked - it could just be the ordering.
User specified 2 controlled protocols:
- **Path A (no-task)**: `Acknowledge -> PostGoodsReceipt`, **never Release**.
- **Path B (task-based)**: identify the `SiteLogisticsRequest` -> call `ReleaseForExecution` on
  IT (not the delivery's own `Release`) -> query `SiteLogisticsLot` -> `ConfirmAsPlanned`.
Required fresh, never-touched STOs only (all old pending STOs in the DB queue turned out to
already be Finished in SAP - confirmed via `find_object_id` on 7 different old deliveries, all
showed `release_status_code`/`delivery_processing_status_code` = `3` already. This itself is a
separate, already-known, still-open discrepancy - see PRD "Vendor GRN POs show full completion in
app, but SAP shows partial/no fulfillment" - our `receipt_status` field just never got told).

### 3a. Fresh test STOs created
- STO-000138 (SAP order 32831): P8 -> P1, `09200726-01`, 1 EA, delivery `P8D1-261`.
- STO-000139 (SAP order 32833): P1 -> P8, `IRON-SCR`, 1 KGM, delivery `P1D1-572`.
Both required the user to manually complete Goods Issue in SAP UI first (this app's GI step is
intentionally manual, no automation - Sep 18 2026 decision, see PRD). Confirmed via raw OData
query that both deliveries were genuinely virgin before any test action: `release_status_code`,
`delivery_processing_status_code`, `delivery_note_status_code` all `"1"` (Not Released / Not
Started / Not Acknowledged). Raw `InboundDeliveryItemQuantity` baseline recorded too: `Quantity`,
`UnitCode`, `QuantityRoleCode=18`, `QuantityOriginCode=4`, `QuantityTypeCode`, `ParentObjectID` -
all normal, matched the requested qty exactly.

### 3b. Path A result - WORKS, no Release needed, on BOTH sites
`Acknowledge` -> `PostGoodsReceipt` directly (via `sap_inbound_delivery_client.post_goods_receipt`,
skipping `release_delivery` entirely) succeeded with HTTP 200 on both P1D1-572 (receiving at P8)
AND P8D1-261 (receiving at P1 - the site previously assumed task-based). SAP itself flipped
`ReleaseStatusCode` to `3` and `DeliveryProcessingStatusCode` to `3` as a side effect of the
successful PGR - Release was never called by us at all.
**Real inventory receipt independently verified via `SAPInventoryClient.get_inventory_detail`
(not just a status flip)**:
- P8-HOLD `IRON-SCR`: 3.2 -> 4.2 KGM (+1 exactly).
- P1-HOLD `09200726-01`: 1.0 -> 2.0 EA (+1 exactly).
Never reached Path B - direct PGR succeeded both times.

### 3c. Root cause conclusion
Calling `Release` BEFORE `PostGoodsReceipt` is what disables PGRBackground for this tenant - not
a blanket dead API. `KBA 3583076` genuinely reproduces, but only for a delivery that's ALREADY
been Released without an immediate GR (the real, narrower state STO-000123/P1D1-560 is stuck in
from much older testing - unrelated to this fix, still open, see PRD backlog "Retest STO-000123").

### 3d. Code fix
`inbound_receipt_service.start_automated_receipt` reordered: direct `PostGoodsReceipt` (no
Release) is now the PRIMARY path for every delivery. The old `Release -> find_recent_lots ->
ConfirmAsPlanned` route is now only a FALLBACK, tried only if the direct PGR attempt itself is
rejected. Both test STOs (000138, 000139) completed end-to-end through the real `/receive`
endpoint afterward (relocation step ran normally) - `receipt_status: "received"` on both, real
GAC IDs (282800, 282828 respectively - exact IDs may differ slightly by run, see git history for
exact values at time of commit).
A 3rd fresh STO (000140, SAP order 32818) was created to verify the rewrite through the actual
endpoint end-to-end but abandoned before Goods Issue completed (removed from app DB; SAP order
32818 still exists in real SAP awaiting manual GI - harmless, user can complete or ignore/cancel).

## 4. Parallelizing multi-line relocation (Part 8 in PRD)
User's ask: 10-line STO, can total time be brought under 10s?
`_relocate_receipt_from_hold` (in `inbound_receipt_service.py`) previously ran one Goods Movement
SOAP call (`_trigger_goods_movement`, in `store_approval_service.py`) per line, sequentially.
Changed to fire all lines concurrently via `ThreadPoolExecutor(max_workers=SAP_MAX_CONCURRENT_
REQUESTS)` (still respects the shared tenant-wide `sap_semaphore` cap of 3 in-flight SAP calls -
this only removes the function's OWN extra serialization on top of that cap). Result order
preserved via `zip(items, futures)`.
Live-verified on a real 3-line STO (STO-000084, P8): 2 lines succeeded (G12NUT/G12FW, real GAC
IDs), 1 correctly failed ("Stock does not exist in the STO warehouse" - genuine pre-existing
depleted P8-HOLD stock for G12LW, unrelated to this change) - correctness of concurrent execution
+ error handling + result-order mapping all confirmed.

## 5. Real-world timing: STO-000142 (user's live test, 8 lines, P9->P2)
User reported "65 seconds, too much" for a 7/8-line receive. Backend's own recorded number:
`receipt_duration_seconds = 29` (job created 21:28:01.595, `receipt_completed_at` 21:28:31.046 -
~29.45s actual). Discrepancy with the 65s the user perceived was never fully explained (asked the
user how they timed it - no answer given before the thread moved on to root-causing the 29s
itself). All 8 lines succeeded, real GAC IDs for every product.

### Root cause of the 29-30s (not a bug - real SAP latency + concurrency math)
Each individual SAP call (Acknowledge, PostGoodsReceipt, each line's Goods Movement) takes
roughly 7-9s round-trip in this tenant (observed, not from official SAP SLA docs - this is empirical
from log timestamps across this session). With `SAP_MAX_CONCURRENT_REQUESTS=3`, 8 relocation
lines need `ceil(8/3) = 3` sequential batches. Plus 2 upfront sequential calls (Acknowledge, PGR)
before the parallel relocation even starts. Rough math: `2 calls x ~7-9s + 3 batches x ~7-9s ~=
35-45s` worst case, `~29s` is consistent with this once some calls land faster than others within
a batch (`ThreadPoolExecutor.result()` on the batch only waits for the slowest one, not the sum).
Also observed: background jobs (BOM cache refresh, SAP Open PO Quantity cache refresh) were
actively running in the SAME window, competing for the same 3-slot `sap_semaphore` - real,
measured contention, not hypothetical (see raw log grep in this session's transcript,
`21:29:05 - SAP Open PO Quantity cache refresh complete`, overlapping the receipt's own window).
**Correction to earlier promise**: the original "<10s for a 10-line STO" estimate assumed ~1-2s
per SAP call - wrong. Real per-call latency here is ~4-5x higher, which invalidates that estimate
regardless of concurrency strategy.

## 6. Option 1 vs Option 2 considered to speed this up further

### Option 1 - HTTP connection/session reuse (safe, NOT YET IMPLEMENTED)
Every call in `sap_goods_movement_client.py` (and the other SAP clients used by this engine) opens
a brand-new `requests.post(...)` connection - no shared `requests.Session`/connection pooling, so
every call pays a fresh TCP+TLS handshake on top of SAP's own processing time. Estimated impact:
modest, ~5-15% total time reduction (a few hundred ms to ~1s per call saved, out of a 7-9s call
where most time is SAP-side backend processing, not our network transport). Not implemented yet -
this was the recommended, safe next step at the point this document was written.

### Option 2 - batch all lines into ONE SOAP call (INVESTIGATED, RULED OUT)
SAP's own schema for `InventoryProcessingGoodsAndActivityConfirmationGoodsMovementIn.
DoGoodsMovementGoodsAndActivityConfirmation` DOES support multiple `<GoodsAndActivityConfirmation>`
root blocks (each with its own `ExternalID`) in ONE SOAP request (confirmed via SAP Help docs:
https://help.sap.com/doc/sap-business-bydesign/2211/en-US/PSM_ISI_R_II_APGACFM_GOODS_MOVEMENT_IN.html).
**Live-tested directly against production** (bypassing our client's single-item envelope builder,
raw hand-built XML) with 2 confirmation blocks targeting the SAME product/site (`09200726-01` at
P1): one valid (1 EA, P1-HOLD -> P1-RM, would succeed on its own - real stock: P1-HOLD had 1.0 EA
before), one deliberately invalid (999999 EA, guaranteed "negative stock not permitted").
**Result: the ENTIRE call failed as a SOAP Fault, HTTP 500** - `faultstring: "Application exception
occurred!"`, `<faultText>Negative stock not permitted in logistics area P1-HOLD, material
09200726-01</faultText>`. The valid 1 EA confirmation was rejected too, purely because it was
bundled with the invalid one. Verified via `SAPInventoryClient` immediately after: P1-RM stayed at
2.0 EA, P1-HOLD stayed at 1.0 EA - no partial posting leaked (clean atomic rollback, no data
corruption), but definitively **no partial success across confirmations in one call** - this
service processes the entire request as one atomic transaction/BOI session.
**Conclusion: Option 2 is unsafe for this use case and was RULED OUT.** Batching would make things
WORSE than today (currently 1 bad line only fails that 1 line - see STO-000084 in #4; batching
would make 1 bad line fail ALL lines in the batch). No safeguard/reconciliation logic can fix this
- it isn't a parsing/attribution problem, it's a hard platform behavior (one SOAP call = one
atomic transaction, no per-item independence).

## 7. Possible additional SAP-side services that MIGHT help (none built, none confirmed)
Investigated whether a different service/protocol could give real per-item independence within
one network round-trip:
- **A custom OData service (via SAP OData Service Explorer, same mechanism already used for
  `khinbounddeliveryexecution`/`SiteLogisticsLot`) exposing a Create-capable entity for goods
  movements** - IF such a BO can be exposed this way, OData's own `$batch` mechanism (OData v2)
  supports multiple independent changesets in ONE HTTP request, each committing/rolling back on
  its OWN, which is architecturally the right primitive for "N independent writes, 1 round-trip,
  partial success allowed" - unlike the SOAP service tested in #6 which processes the whole
  payload as one BOI session. **This is UNVERIFIED and UNBUILT**: (a) unclear if the underlying
  BO behind Goods and Activity Confirmation can even be exposed for OData Create via Service
  Explorer (it may be locked to SOA-only technical objects), (b) even if exposed, changeset
  independence would need to be live-tested the same way #6 was, not assumed. Would require the
  user's SAP admin to build this - same class of ask as the earlier `SiteLogisticsLot` service.
- No evidence found of an existing standard OData service already exposing this with the needed
  semantics - only the SOAP inbound service tested in #6, and the read-only Inventory Report
  OData service (`SAPInventoryClient`) already in use.
- No async/queued variant of `DoGoodsMovementGoodsAndActivityConfirmation` exists - SAP's own docs
  confirm this specific operation is synchronous only.

## 8. Current recommendation (as of writing this doc)
1. Implement Option 1 (connection/session reuse) - safe, modest win, NOT YET DONE.
2. Do not pursue Option 2 (SOAP batching) - proven unsafe.
3. If more speed is needed later, the only other lever without new SAP-side work is raising
   `SAP_MAX_CONCURRENT_REQUESTS` above 3 - NOT done, flagged as a shared tenant-wide risk (could
   affect background jobs / hit SAP's own throttling) requiring careful, deliberate testing, not a
   casual change.
4. A genuinely bigger win (per-item independent batching) would require a NEW custom OData
   service from the SAP admin - unbuilt, unverified, a real ask for the user if they want to
   pursue it.
