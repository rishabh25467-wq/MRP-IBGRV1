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

RESOLVED Sep 14 2026 (was "KNOWN LIMITATION" - a vendor's own open PO
that was unusually old could fall outside the live recency window and
never show up; real incident, vendor P3267): fetch_backfill_chunk (see
BACKFILL_WATERMARK_COLLECTION below) walks the older ID range in the
background, independent of the live window, so an old-but-still-open PO
now gets (re)cached rather than aging out permanently. A SEPARATE,
deeper bug was found and fixed the same day - see WINDOW_WIDTH_IDS /
_ID_BETWEEN_TEMPLATE below for the full root cause (the open-ended `>`
query itself was silently dropping real records once truncated).

SAP does not expose a per-item "already delivered" quantity on this
query (only header-level status codes) - `already_shipped_qty` /
`remaining_qty` are computed entirely from OUR OWN
`supplier_portal_shipments` history (see supplier_shipment_service.py),
same as before this endpoint existed. "Open" here means
`DeliveryProcessingStatusCode` is not yet `3` (Finished), AND
`PurchaseOrderLifeCycleStatusCode` is not `8` (Cancelled, Sep 13 2026
fix), AND `ApprovalStatusCode` is not `1` (still In Preparation/not yet
released, Sep 13 2026 follow-up fix - see NOT_YET_RELEASED_APPROVAL_STATUS_CODE)."""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

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
# Sep 13 2026 follow-up bug fix (user's question: "Why is it visible to
# supplier in their vendor control" - re: a PO still "In Preparation" in
# SAP): confirmed live in a real 500-ID window that `ApprovalStatusCode`
# "1" (not yet approved/released internally) pairs ONLY with
# (LifeCycle=1, Delivery=1) - a genuine draft PO, never sent to the
# supplier by the buyer yet - across 42 real POs found in this state.
# Every other PO in the same window (open, finished, cancelled) had
# `ApprovalStatusCode` "2" (Released). This client never checked this
# field at all, so a still-in-draft PO was fully visible/shippable-
# against on the Supplier Dashboard the moment it existed in SAP, well
# before the buyer had actually approved/released it.
NOT_YET_RELEASED_APPROVAL_STATUS_CODE = "1"
# Sep 14 2026 follow-up (user's explicit ask, after confirming the
# visible PO list was otherwise all genuinely valid/Released POs):
# SAP's own `PurchaseOrderLifeCycleStatusCode` code list also has a
# "4" = Rejected state (buyer/approver rejected the PO outright,
# distinct from "8"=Canceled) - not yet seen live on this tenant in
# the window checked, but excluded proactively so a Rejected PO can
# never surface on the Supplier Dashboard as if it were still open.
REJECTED_LIFECYCLE_STATUS_CODE = "4"

# Sep 14 2026, user's explicit ask ("need to see status of PO for
# now") - full SAP ByDesign PurchaseOrderLifeCycleStatusCode code list
# (confirmed via SAP's own web service docs), used to attach a
# human-readable status onto every cached row - note that codes 1/4/8
# (In Preparation/Rejected/Canceled) are excluded above BEFORE caching,
# so a cached row will only ever show one of the other, still-open
# states below - kept here anyway for completeness/no-guessing.
LIFECYCLE_STATUS_TEXT = {
    "1": "In Preparation", "2": "In Approval", "3": "In Revision", "4": "Rejected",
    "5": "Not Yet Acknowledged", "6": "Sent", "7": "Acknowledgment Received",
    "8": "Canceled", "9": "Follow-Up Document Created", "10": "Finished",
}

# The buying company legal entity for a PO (`PartyBuyerPartyKey/PartyID`,
# e.g. "RI") - same 2-entity setup already used elsewhere in this app
# (see stock_transfer_service.py's identical mapping) - added for the
# Supplier Portal PO table's "PO From" column (Aug 28 2026, user's ask).
BUYER_ENTITY_NAMES = {"RI": "RAY INTERNATIONAL", "RT": "RADISH TECHNOLOGIES"}


def buyer_entity_name(buyer_code: str) -> str:
    return BUYER_ENTITY_NAMES.get(buyer_code, "RADISH TECHNOLOGIES")

WATERMARK_COLLECTION = "sap_po_watermark"
# Kept a bit under this client's WINDOW_WIDTH_IDS so the very
# first-ever discovery has a sane initial lower bound before any
# watermark exists yet.
LOOKBACK_IDS = 450

# Sep 14 2026 CRITICAL bug fix (real incident, user report: vendor P3267
# had 5 genuinely "In Process" POs in SAP - 27601/28255/28467/29027/
# 29073 - silently vanished from the Supplier Dashboard even with "All
# (incl. Fully Shipped)" selected). TWO root causes found, confirmed
# live against this tenant's real SOAP responses:
#
# 1) The open-ended `>` query (`IntervalBoundaryTypeCode=8`, previously
#    the ONLY query shape this client used) does NOT reliably return
#    "the next N records in ID order" once the true match count beyond
#    the threshold exceeds its own `QueryHitsMaximumNumberValue` - which
#    is ALWAYS true in production, since there are thousands of POs
#    above any given watermark. Confirmed live: querying
#    `PurchaseOrderID > 27000` with limit=500 returned a batch spanning
#    ID 27016 all the way to 29570 (a 2554-wide span) while silently
#    OMITTING 27601/28255/29027/29073 from right in the middle of that
#    same span - not a simple "lowest N" cutoff, an effectively
#    arbitrary subset once truncated.
# 2) A bounded `Between` query (`IntervalBoundaryTypeCode=3`, both a
#    Lower AND Upper boundary) IS reliable - confirmed live with a
#    narrow enough window (a 200-ID-wide Between returned exactly 199
#    real POs, including the previously-missing 29027) that its true
#    hit count stays comfortably under the limit. `WINDOW_WIDTH_IDS`
#    below is deliberately kept well under `BETWEEN_QUERY_LIMIT` given
#    this tenant's real, live-confirmed worst-case density of ~1 real
#    PO per sequential ID.
#
# FIX: every fetch (both the live recent-window scan and the older-PO
# backfill below) now ALWAYS walks the ID range in fixed
# `WINDOW_WIDTH_IDS`-wide `Between` chunks - never a single open-ended
# `>` call - so nothing in the scanned range can ever be silently
# dropped again.
WINDOW_WIDTH_IDS = 400
BETWEEN_QUERY_LIMIT = 999

# This collection is a SEPARATE, independent watermark that slowly walks
# FORWARD from a starting floor (never backwards, never overlapping the
# live "recent window" watermark above) to backfill/re-verify every
# older PO at least once, so a still-open old PO that has aged out of
# the live recent-window scan gets (re)cached instead of aging out
# permanently and being wrongly marked `expired`.
BACKFILL_WATERMARK_COLLECTION = "sap_po_backfill_watermark"
# On the very first run (no backfill watermark yet), start this far
# behind the live window's own floor rather than all the way back at PO
# ID 0 - walking the tenant's ENTIRE history (years of long-Finished
# POs) is wasted SAP load for no benefit. 5000 is >10x LOOKBACK_IDS, so
# it comfortably covers this real incident (P3267's oldest affected PO,
# 27601, was ~1521 IDs behind the live window at the time) with a lot
# of headroom.
BACKFILL_INITIAL_DEPTH_IDS = 5000

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

# Sep 14 2026 addition - see the fix note above. Only ever used with a
# `LowerBoundaryID`/`UpperBoundaryID` pair narrow enough
# (`WINDOW_WIDTH_IDS`) that its true hit count can never approach
# `limit`, unlike the open-ended template above which this replaces for
# all real data-fetching (the `>` template is still used, alone, only
# for the cheap limit=1 existence probes in `_has_po_id_greater_than`).
_ID_BETWEEN_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n1:PurchaseOrderSimpleByElementsQuery_sync xmlns:n1="http://sap.com/xi/SAPGlobal20/Global">
 <PurchaseOrderSimpleSelectionByElements>
  <SelectionByID>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>3</IntervalBoundaryTypeCode>
   <LowerBoundaryID>{lower_bound}</LowerBoundaryID>
   <UpperBoundaryID>{upper_bound}</UpperBoundaryID>
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

    def _discover_current_max_po_id(self, start_lo: int = 0) -> int:
        """Cheap probes only (limit=1 each) - exponential search to find
        an upper bound with no POs, then binary search down to the exact
        current max PurchaseOrderID. `start_lo` lets `fetch_recent_window`
        below resume the search from its last known watermark instead of
        0 every cycle - typically only a few new POs exist since the last
        cycle, so this stays cheap (a handful of probes, not ~15)."""
        lo, hi = start_lo, start_lo + 1000
        while self._has_po_id_greater_than(hi):
            lo, hi = hi, hi * 2 if hi > 0 else 1000
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._has_po_id_greater_than(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _fetch_between(self, lower_bound: int, upper_bound: int) -> tuple:
        """Single reliable, bounded fetch - see the Sep 14 2026 fix note
        above. Caller MUST keep (upper_bound - lower_bound) <=
        WINDOW_WIDTH_IDS."""
        body = _ID_BETWEEN_TEMPLATE.format(lower_bound=lower_bound, upper_bound=upper_bound, limit=BETWEEN_QUERY_LIMIT)
        root = ET.fromstring(self._post(body).text)
        return self._parse_pos(root)

    def _scan_between(self, lower_bound: int, ceiling: int) -> list:
        """Walks (lower_bound, ceiling] in WINDOW_WIDTH_IDS-wide reliable
        Between chunks and returns every row found across all of them."""
        rows = []
        cursor = lower_bound
        while cursor < ceiling:
            upper = min(cursor + WINDOW_WIDTH_IDS, ceiling)
            chunk_rows, _ = self._fetch_between(cursor + 1, upper)
            rows.extend(chunk_rows)
            cursor = upper
        return rows

    @staticmethod
    def _parse_pos(root, keep_filtered_out: bool = False) -> tuple:
        """Shared row-parsing/filtering logic used by both the live
        "recent window" fetch and the older-PO backfill chunk below -
        extracted Sep 14 2026 so the two never drift apart. Returns
        (rows, max_id_seen_across_ALL_pos_in_this_batch) - max_id_seen
        deliberately includes Finished/Cancelled/filtered-out POs too,
        since the caller needs it purely to advance an ID pointer, not
        to judge openness."""
        rows = []
        max_id_seen = 0
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
                or po.findtext("PurchaseOrderLifeCycleStatusCode") == REJECTED_LIFECYCLE_STATUS_CODE
                or po.findtext("ApprovalStatusCode") == NOT_YET_RELEASED_APPROVAL_STATUS_CODE
            ):
                continue
            po_date = po.findtext("SystemAdministrativeData/CreationDateTime")
            buyer_code = po.findtext("PartyBuyerPartyKey/PartyID")
            currency = po.findtext("CurrencyCode")
            lifecycle_status_code = po.findtext("PurchaseOrderLifeCycleStatusCode")
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
                    "lifecycle_status_code": lifecycle_status_code,
                    "lifecycle_status_text": LIFECYCLE_STATUS_TEXT.get(lifecycle_status_code, lifecycle_status_code),
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
        return rows, max_id_seen

    def fetch_recent_window(self, db) -> dict:
        """Called ONLY from server.py's background refresh loop, never
        from a live request (see module docstring, "THIRD FIX"). Fetches
        every PO created since the last cycle (SAP won't filter by
        vendor server-side anyway - see fix #1) and returns each open
        line item tagged with its real `vendor_code`
        (PartySellerPartyKey/PartyID) so the caller can fan results out
        per vendor.

        Sep 14 2026 rewrite: now scans in reliable, bounded `Between`
        chunks (see WINDOW_WIDTH_IDS / the fix note above) instead of a
        single open-ended `>` call - that call could (and did, in
        production) silently skip real open POs once the true remaining
        count exceeded its own limit. `current_max` is (re)discovered
        every cycle, but starting the binary search from the LAST known
        watermark (not 0) keeps it cheap - usually only a couple of
        probes since only a handful of new POs exist since the last
        10-minute cycle.

        Also returns the `lower_bound` used for this fetch - callers
        need this to avoid wrongly expiring a cached PO that's merely
        OLDER than this window rather than genuinely gone from SAP (see
        BACKFILL_WATERMARK_COLLECTION above)."""
        if not self.endpoint:
            raise SAPPurchaseOrderNotConfiguredError(
                "SAP Purchase Order lookup isn't wired up yet - waiting on the SAP SOAP/OData "
                "endpoint (SAP_SOAP_PO_ENDPOINT) to be activated and shared for this tenant."
            )
        state = db[WATERMARK_COLLECTION].find_one({"_id": "latest"})
        if state:
            lower_bound = state["max_po_id"]
            current_max = self._discover_current_max_po_id(start_lo=lower_bound)
        else:
            current_max = self._discover_current_max_po_id()
            lower_bound = max(0, current_max - LOOKBACK_IDS)
        rows = self._scan_between(lower_bound, current_max)
        db[WATERMARK_COLLECTION].update_one(
            {"_id": "latest"},
            {"$set": {"max_po_id": current_max, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return {"rows": rows, "lower_bound": lower_bound}

    def fetch_backfill_chunk(self, db) -> dict:
        """Sep 14 2026 fix - walks FORWARD from a persisted floor in
        WINDOW_WIDTH_IDS-wide reliable `Between` chunks, independent of
        and never overlapping `fetch_recent_window`'s own forward-moving
        watermark above, to (re)confirm every PO older than the live
        window at least once. Any still-open PO found this way is
        upserted into the cache (see supplier_shipment_service.
        merge_backfill_rows) WITHOUT expiring anything else for that
        vendor - a partial ID-range chunk can never be treated as "the
        vendor's whole PO list" the way a full fetch_recent_window batch
        can.

        Sep 18 2026 fix (real incident - PO 29654 never appeared on
        vendor S3772's dashboard despite being genuinely open): once the
        floor first caught up to the ceiling this used to permanently
        stop (`done=True` forever) - but a PO's status can legitimately
        flip from filtered-out (e.g. "Not Yet Released") to genuinely
        open SOMETIME AFTER its own ID range was already consumed by a
        one-time scan, and nothing ever looked at that range again.
        Now, once caught up, it simply restarts from
        `ceiling - BACKFILL_INITIAL_DEPTH_IDS` and keeps re-sweeping that
        recent history indefinitely - `done` is kept in the return value
        only to log a friendly "completed a full lap" message, it no
        longer means "stop calling this"."""
        ceiling_doc = db[WATERMARK_COLLECTION].find_one({"_id": "latest"})
        ceiling = (ceiling_doc or {}).get("max_po_id", 0)
        if not self.endpoint or ceiling <= 0:
            return {"rows": [], "done": True}
        floor_doc = db[BACKFILL_WATERMARK_COLLECTION].find_one({"_id": "latest"})
        floor = (floor_doc or {}).get("floor_id") if floor_doc else max(0, ceiling - BACKFILL_INITIAL_DEPTH_IDS)
        if floor is None:
            floor = max(0, ceiling - BACKFILL_INITIAL_DEPTH_IDS)
        if floor >= ceiling:
            floor = max(0, ceiling - BACKFILL_INITIAL_DEPTH_IDS)
            db[BACKFILL_WATERMARK_COLLECTION].update_one(
                {"_id": "latest"}, {"$set": {"floor_id": floor, "updated_at": datetime.now(timezone.utc)}}, upsert=True,
            )
            return {"rows": [], "done": True}
        upper = min(floor + WINDOW_WIDTH_IDS, ceiling)
        rows, _ = self._fetch_between(floor + 1, upper)
        db[BACKFILL_WATERMARK_COLLECTION].update_one(
            {"_id": "latest"},
            {"$set": {"floor_id": upper, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return {"rows": rows, "done": upper >= ceiling, "floor": upper, "ceiling": ceiling}
