"""SAP Business ByDesign Purchase Order query client (Supplier Portal
Phase 1, Aug 2026) - lets an approved external vendor see their OWN open
Purchase Orders inside the Supplier Portal.

Live-verified Aug 26 2026 against this tenant's real
`QueryPurchaseOrderQueryIn` service (SAP_SOAP_PO_ENDPOINT) using the same
`_EMERGENTBOM` technical user every other SOAP client in this app uses -
message `PurchaseOrderSimpleByElementsQuery_sync`.

BUG FOUND + FIXED same day (user report: "the POs loaded for Hamidi do
not seem correct"): `<SelectionBySellerPartyID>` is SILENTLY IGNORED by
this service on this tenant - passing a real vendor code, a wrong one,
or a nonsense one all returned the exact same unfiltered set of open POs
across DOZENS of unrelated suppliers (confirmed: querying as vendor
H1330 "Hamidi Exports" was actually showing "Ganesh Steel Industries"
(G1287) and ~50 other suppliers' orders - a real cross-vendor data leak,
not just wrong demo data). FIX #1: the ONLY filter actually trusted for
vendor identity is a CLIENT-SIDE check against `PartySellerPartyKey/
PartyID` on every returned PO header.

SECOND BUG FOUND + FIXED Aug 28 2026 (user report: "the pos that loaded
for hamidi do not seem to be correct" - persisted even after fix #1):
this service ignores any ordering hint and always returns records
starting from the LOWEST `PurchaseOrderID` first. With a tenant-wide
batch capped at ~500-700 records (see "communication timeout" note
below), that meant every single fetch - for every vendor - only ever
saw a batch of the OLDEST open POs in the whole system (confirmed live:
an unbounded fetch returned PurchaseOrderID 3927-4426, all with 2024-05
delivery dates, even in Aug 2026). Hamidi's genuinely current open POs
(ID ~28000+, Aug 2026 delivery dates) were never reachable. FIX #2:
`<SelectionByID>` (unlike SelectionBySellerPartyID) IS reliably honored
by this tenant - confirmed live with an `IntervalBoundaryTypeCode=8`
("greater than") probe. So instead of an unbounded query, every fetch
now asks for `PurchaseOrderID > watermark`, where `watermark` is a small
persisted pointer (`sap_po_watermark` Mongo doc) kept just behind the
tenant's current max PO ID - see `_get_lower_bound` and
`_advance_watermark` below.

THIRD FIX, same day: a single fetch (discovery probe + the batch itself)
can take 60-100s+ - too slow to run inside a live HTTP request (the
Kubernetes ingress itself returned a 502 after ~60s when this was tried
inline). So this client is no longer called directly from the request
path at all - `fetch_recent_window` is called ONLY from a periodic
background loop (server.py's `start_supplier_po_cache_refresh_loop`,
same established pattern as bom_cache_service/inventory_service), which
fetches the current window ONCE for ALL vendors together (SAP doesn't
filter by vendor anyway - see fix #1) and fans the results out into
`supplier_portal_po_cache` per vendor via
`supplier_shipment_service.refresh_po_cache`. The `/purchase-orders`
endpoint itself now only ever reads that Mongo cache - instant, no SAP
call, no timeout risk.

Two more things confirmed live:
  1. This tenant's response DOES include full `<PurchaseOrderItem>`
     line-item detail (ItemID, Quantity, Description, ProductKey/
     ProductID, DeliveryPeriod) directly - no second `ManagePurchaseOrderIn`
     Read call needed at all.
  2. The vendor's SAP Vendor Code (what the vendor types in at signup,
     `supplier_portal_accounts.vendor_code`) IS the same value as
     `PartySellerPartyKey/PartyID` - confirmed against the real supplier
     master (H1330 = Hamidi Exports, G1287 = Ganesh Steel Industries).

KNOWN LIMITATION (not fully solved, flagged for the user): since SAP
won't filter server-side by vendor, we still pull a bounded BATCH of the
whole tenant's POs (now a RECENT batch, not an arbitrary one) and filter
client-side. `LOOKBACK_IDS` (below) is deliberately kept a bit under
`FETCH_LIMIT` so a single batch reliably reaches the tenant's current
max PO ID, but a vendor's own open PO that is unusually old (e.g. opened
2+ years ago and never marked Finished in SAP) will fall outside this
recent window and won't show up. If that turns out to matter for a real
vendor, the fix is a proper Analytics OData report (same pattern already
used for HSN Code / On-Hand Inventory - see PRD) - it's server-filterable
by seller party and wouldn't need this recency window at all.

SAP does not expose a per-item "already delivered" quantity on this
query (only header-level status codes) - `already_shipped_qty` /
`remaining_qty` are computed entirely from OUR OWN
`supplier_portal_shipments` history (see supplier_shipment_service.py),
same as before this endpoint existed. "Open" here means
`DeliveryProcessingStatusCode` is not yet `3` (Finished) AND
`PurchaseOrderLifeCycleStatusCode` is not `8` (Cancelled, added Sep 13
2026 fix - see CANCELLED_LIFECYCLE_STATUS_CODE)."""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

SOAP_ACTION = ""
FINISHED_DELIVERY_STATUS_CODE = "3"
# Sep 13 2026 bug fix (real incident, user report: "PO 29480 cancel
# status in the SAP but its showing pending in the supplier dashboard" -
# confirmed live against this tenant's real response for PO 29480:
# `PurchaseOrderLifeCycleStatusCode` is 8 on a cancelled PO (with
# `DeliveryProcessingStatusCode` 4, "Not Relevant" - NOT the "3"/Finished
# code this client already excluded), so a cancelled PO was never
# filtered out at all and kept showing as an open/pending PO to the
# vendor. Cross-checked against every other PO in a 500-ID window: the
# (LifeCycle=8, Delivery=4) pair was unique to exactly the 3 POs SAP
# itself shows as "Canceled" (29480, 29275, 29179) - every other
# combination seen was a genuinely open/in-process/finished PO.
CANCELLED_LIFECYCLE_STATUS_CODE = "8"

# The buying company legal entity for a PO (`PartyBuyerPartyKey/PartyID`,
# e.g. "RI") - same 2-entity setup already used elsewhere in this app
# (see stock_transfer_service.py's identical mapping) - added for the
# Supplier Portal PO table's "PO From" column (Aug 28 2026, user's ask).
BUYER_ENTITY_NAMES = {"RI": "RAY INTERNATIONAL", "RT": "RADISH TECHNOLOGIES"}


def buyer_entity_name(buyer_code: str) -> str:
    return BUYER_ENTITY_NAMES.get(buyer_code, "RADISH TECHNOLOGIES")

WATERMARK_COLLECTION = "sap_po_watermark"
WATERMARK_STALE_AFTER = timedelta(days=3)
# Kept a bit under FETCH_LIMIT so one batch starting at the watermark
# reliably reaches the tenant's current max PO ID (PurchaseOrderID is
# densely sequential on this tenant - confirmed live, ~1 PO per ID).
LOOKBACK_IDS = 450

_ID_GREATER_THAN_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n1:PurchaseOrderSimpleByElementsQuery_sync xmlns:n1="http://sap.com/xi/SAPGlobal20/Global">
 <PurchaseOrderSimpleSelectionByElements>
  <SelectionByID>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>8</IntervalBoundaryTypeCode>
   <LowerBoundaryID>{lower_bound}</LowerBoundaryID>
  </SelectionByID>
 </PurchaseOrderSimpleSelectionByElements>
 <ProcessingConditions>
  <QueryHitsMaximumNumberValue>{limit}</QueryHitsMaximumNumberValue>
  <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
 </ProcessingConditions>
</n1:PurchaseOrderSimpleByElementsQuery_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPPurchaseOrderError(Exception):
    pass


class SAPPurchaseOrderNotConfiguredError(SAPPurchaseOrderError):
    """No SAP endpoint has been wired up yet - distinct from a live call
    that fails, so callers/UI can show "not connected yet" rather than a
    generic error."""
    pass


class SAPPurchaseOrderClient:
    FETCH_LIMIT = 500
    REQUEST_TIMEOUT_SECONDS = 120

    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint or None
        self.auth = HTTPBasicAuth(username, password)

    def _post(self, body: str):
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint, auth=self.auth, data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
        except requests.exceptions.RequestException as e:
            raise SAPPurchaseOrderError(f"Could not reach SAP: {e}")
        if resp.status_code != 200 or "Fault" in resp.text[:2000]:
            raise SAPPurchaseOrderError(f"SAP Purchase Order query failed (HTTP {resp.status_code}): {resp.text[:400]}")
        return resp

    def _has_po_id_greater_than(self, threshold: int) -> bool:
        body = _ID_GREATER_THAN_TEMPLATE.format(lower_bound=threshold, limit=1)
        root = ET.fromstring(self._post(body).text)
        return root.find(".//PurchaseOrder") is not None

    def _discover_current_max_po_id(self) -> int:
        """Cheap probes only (limit=1 each) - exponential search to find
        an upper bound with no POs, then binary search down to the exact
        current max PurchaseOrderID. Only runs when the watermark is
        missing or stale (see _get_lower_bound)."""
        lo, hi = 0, 1000
        while self._has_po_id_greater_than(hi):
            lo, hi = hi, hi * 2
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._has_po_id_greater_than(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _get_lower_bound(self, db) -> int:
        state = db[WATERMARK_COLLECTION].find_one({"_id": "latest"})
        now = datetime.now(timezone.utc)
        if state and (now - state["updated_at"]) < WATERMARK_STALE_AFTER:
            return state["max_po_id"]
        current_max = self._discover_current_max_po_id()
        return max(0, current_max - LOOKBACK_IDS)

    def _advance_watermark(self, db, max_id_seen: int) -> None:
        """Monotonic: an empty/no-progress SAP batch must NEVER move
        max_po_id backwards (found by testing_agent, iteration 122 - the
        old unconditional write walked the window back 450 IDs on every
        empty cycle, eventually reproducing the original oldest-PO bug).
        `updated_at` still always refreshes so callers can tell the
        background loop is alive even on an empty cycle."""
        state = db[WATERMARK_COLLECTION].find_one({"_id": "latest"})
        new_lower_bound = max(0, max_id_seen - LOOKBACK_IDS)
        if state and new_lower_bound <= state.get("max_po_id", 0):
            db[WATERMARK_COLLECTION].update_one(
                {"_id": "latest"}, {"$set": {"updated_at": datetime.now(timezone.utc)}}, upsert=True,
            )
            return
        db[WATERMARK_COLLECTION].update_one(
            {"_id": "latest"},
            {"$set": {"max_po_id": new_lower_bound, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    def fetch_recent_window(self, db) -> list:
        """Called ONLY from server.py's background refresh loop, never
        from a live request (see module docstring, "THIRD FIX"). Fetches
        the tenant's current PurchaseOrderID window ONCE for every
        vendor at once (SAP won't filter by seller party server-side
        anyway) and returns each open line item tagged with its real
        `vendor_code` (PartySellerPartyKey/PartyID) so the caller can
        fan results out per vendor."""
        if not self.endpoint:
            raise SAPPurchaseOrderNotConfiguredError(
                "SAP Purchase Order lookup isn't wired up yet - waiting on the SAP SOAP/OData "
                "endpoint (SAP_SOAP_PO_ENDPOINT) to be activated and shared for this tenant."
            )
        lower_bound = self._get_lower_bound(db)
        body = _ID_GREATER_THAN_TEMPLATE.format(lower_bound=lower_bound, limit=self.FETCH_LIMIT)
        root = ET.fromstring(self._post(body).text)

        rows = []
        max_id_seen = lower_bound
        for po in root.findall(".//PurchaseOrder"):
            po_number = po.findtext("PurchaseOrderID")
            try:
                max_id_seen = max(max_id_seen, int(po_number))
            except (TypeError, ValueError):
                pass
            vendor_code = po.findtext("PartySellerPartyKey/PartyID")
            if (
                not vendor_code
                or po.findtext("DeliveryProcessingStatusCode") == FINISHED_DELIVERY_STATUS_CODE
                or po.findtext("PurchaseOrderLifeCycleStatusCode") == CANCELLED_LIFECYCLE_STATUS_CODE
            ):
                continue
            po_date = po.findtext("SystemAdministrativeData/CreationDateTime")
            buyer_code = po.findtext("PartyBuyerPartyKey/PartyID")
            currency = po.findtext("CurrencyCode")
            vendor_name = None
            for item in po.findall("PurchaseOrderItem"):
                qty_el = item.find("Quantity")
                due = item.findtext("DeliveryPeriod/EndDateTime")
                unit_price_el = item.find("NetUnitPrice/Amount")
                subtotal_el = item.find("NetAmount")
                if vendor_name is None:
                    vendor_name = item.findtext("ShipFromLocation/AddressSnapshot/FormattedAddress/FormattedName")
                rows.append({
                    "po_number": po_number,
                    "item_number": item.findtext("ItemID"),
                    "vendor_code": vendor_code,
                    "vendor_name": vendor_name,
                    "product_id": (item.findtext("ItemProduct/ProductKey/ProductID") or "").strip() or None,
                    "description": item.findtext("Description"),
                    "po_qty": float(qty_el.text) if qty_el is not None and qty_el.text else 0.0,
                    "unit_of_measure": qty_el.get("unitCode") if qty_el is not None else None,
                    "due_date": due.split("T")[0] if due else None,
                    "po_date": po_date.split("T")[0] if po_date else None,
                    "buyer_code": buyer_code,
                    "currency": currency,
                    "unit_price": float(unit_price_el.text) if unit_price_el is not None and unit_price_el.text else None,
                    "subtotal": float(subtotal_el.text) if subtotal_el is not None and subtotal_el.text else None,
                    # Sep 2 2026 fix (user report: "why is SITE not fixed in
                    # GRN?"): the item's own real SAP ship-to Site/plant -
                    # buyer_code (RI/RT) alone can't pin an exact site since
                    # an entity owns MULTIPLE sites (e.g. RI = P1 and P8), so
                    # GRN's Site field could only narrow to 2 choices, never
                    # auto-lock to 1. This is the exact SAP field the PO
                    # itself was actually placed against - confirmed live on
                    # this tenant's real PurchaseOrderItem schema.
                    "ship_to_site_id": item.findtext("ShipToLocation/LocationID"),
                })
        self._advance_watermark(db, max_id_seen)
        return rows
