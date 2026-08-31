"""SAP Business ByDesign custom OData service `odataoutboundemergent` -
finds the Outbound Delivery Request SAP produces from our own Customer
Requirement (Stock Transfer Order) and posts the Goods Issue on it, fully
automatically (Aug 27 2026, user's explicit ask - "we need full", no extra
step for the user to see/click).

Discovered live (this app's own tenant) via a sister app's documentation
that this exact OData service + PGIInBackground action already works on
this SAME tenant. The hard part was SAFELY linking OUR Customer
Requirement to the Outbound Delivery Request it produces:
  - `BaseBusinessTransactionDocumentID` on OutboundDeliveryRequest looked
    promising but is NOT a reference to the source document at all - it's
    the ODR's own self-classification ("68" = Logistics Execution
    Request, confirmed via this tenant's own live code list) and its
    short numeric ID collides with unrelated years-old documents.
  - The real, safe link is `OutboundDeliveryRequestItemBusinessTransactionDocumentReferenceSalesOrderCollect`
    - misleadingly named "SalesOrder" (that's just what the OTHER app
    that built this custom service happened to name its one nav
    property) but the underlying table holds a `TypeCode`+`ID`+`UUID`
    triple for ANY predecessor document type. Verified live: filtering
    this collection by `UUID eq guid'<our Customer Requirement's real
    UUID>'` returns exactly one row with `TypeCode` "814" = "Stock
    Transfer Order" (confirmed via this tenant's own code list) and the
    right ID/product/site every time - a 100%, zero-collision match.

Auth: HTTPS Basic with a ByD Business User - this tenant's existing
SAP_USERNAME/SAP_PASSWORD ("itadmin", otherwise unused elsewhere in this
app) already confirmed live to have the right authorization. Custom OData
services reject Communication Users (the type every other SAP client in
this app uses), which is why this needs its own separate credentials.

Writes (PGIInBackground) need an x-csrf-token fetched via a prior GET
with header `X-CSRF-Token: Fetch`, reused with that same session's
cookies on the POST - standard SAP OData CSRF pattern.

GST / E-way bill fields (Aug 2026 session): this service's
`OutboundDeliveryRequestCollection`/`OutboundDeliveryCollection` DO carry
the 5 custom `_KUT` fields the user needs (`TransportationMode_KUT`,
`VehicleNo_KUT`, `PlaceOfSupply_KUT`, `GRNo1_KUT`, `DateOfSupply_KUT`,
all `sap:creatable/updatable="true"`) - but confirmed live, on 2 real
orders, at 2 different lifecycle stages, that SAP hard-locks the whole
document for ANY field write via API the instant its own scheduler picks
it up (before this app's job can ever reach it) - "Changing data not
possible; data is read-only" on both the Request and the final released
Delivery. No Key User Extension Scenario exists to carry a field forward
from Customer Requirement either (checked live). Real working fix:
`sap_sto_client.py`'s `GST_NOTE_TYPE_CODE` writes these values as a
standard SAP Note on the Customer Requirement, at CREATE time (zero race
condition) - see stock_transfer_service.py's `_build_gst_note_text`.

Aug 27 2026, "one delivery per multi-line order" investigation (real
incident: STO-000046, SAP order 30336, 3 lines - every line correctly
left the source site, but SAP created 3 SEPARATE Outbound Deliveries,
P8D1-185/186/187, instead of the ONE combined delivery the user's
business expects). 3 live attempts were tried at THIS layer before
finding the real fix elsewhere:
  - Grouping by the shared `ParentObjectID` (every line's Outbound
    Delivery Request ITEM shares one common header ObjectID) and
    calling PGIInBackground once per document - rejected outright:
    "Action PGIInBackground not possible; object does not exist",
    confirmed via this service's own $metadata that PGIInBackground's
    EntitySet is `OutboundDeliveryRequestItemCollection` (item-scoped
    only, no header-level call possible through it).
  - `SLRequestDeliveryExecution` (bound to
    `OutboundDeliveryRequestItemScheduleLineCollection`, matching SAP's
    own documented pattern for combining several ScheduleLines into one
    delivery) - rejected: "Malformed URI literal syntax" (its ObjectID
    param apparently can't carry more than one ID the way this app
    tried encoding it).
  - `OutboundDeliveryRequestAllocate` (header-scoped, bound to
    `OutboundDeliveryRequestCollection`) - rejected: "Project outbound
    delivery request reference missing or not valid", suggesting this
    action is built for a different (project-based) SAP scenario.
THE STO-LEVEL FIX WAS NECESSARY BUT NOT SUFFICIENT: setting
`CompleteDeliveryRequestedIndicator=true` in `sap_sto_client.py` DOES
make SAP combine every line of a new multi-line order into one single
Outbound Delivery Request document (confirmed live, order 30411 - both
lines share the same ParentObjectID). But `PGIInBackground` itself
(confirmed via this service's own $metadata: bound to
`OutboundDeliveryRequestItemCollection`, `ObjectID` is a single
`Edm.String` - no batch/multi-item parameter exists on this action at
all) still only ever takes ONE item per call - calling it once per line
(as this file always has) posts+creates+releases ITS OWN Outbound
Delivery each time regardless of the combined request underneath,
confirmed live: order 30411's 2 lines, one shared Outbound Delivery
Request, still ended up as 2 separate Deliveries (P1D1-481/482).

Attempt #5 (Aug 27 2026, user's explicit ask to keep trying): stop
auto-releasing each item's delivery the instant its own GI posts
(`AutoReleaseOutboundDelivery=false` now, always) and instead release
explicitly, once, only after checking what Delivery object(s) every
line in this poll batch actually resolved to
(`find_outbound_delivery_objects` below, `release_outbound_delivery`
below, both driven from stock_transfer_service.py's
`_release_ready_deliveries`) - the hypothesis being that leaving a
delivery "open" (unreleased) may let a same-order sibling line's own GI
call attach into it instead of spawning a new one, the same way SAP's
own standard collective-delivery-processing works (create → add
eligible items → release, rather than create-and-immediately-finalize
per item). CONFIRMED LIVE (STO-000052, Aug 27 2026): still 3 separate
Deliveries - this hypothesis was wrong, kept anyway since it's still a
correct/safe release mechanism on its own.

Attempt #6 (Aug 27 2026, real re-check of $metadata after attempt #3's
rejection): the earlier `SLRequestDeliveryExecution` attempt sent NO
value at all for `TargetSiteLogisticsRequestUUID` (a REQUIRED param on
this specific action, confirmed via $metadata - easy to have missed,
it's not on the item-level `PGIInBackground` this file otherwise always
uses) - the "Malformed URI literal syntax" fault was very likely this
missing/empty GUID literal, not the multi-ID-joining that was originally
suspected. Re-tested live (Aug 27 2026, order 30421) with a real,
syntactically-valid shared GUID for every line - CONFIRMED DEAD END:
SAP now returns a clean business fault, "Site logistics request does
not exist", meaning this param must reference an ALREADY-EXISTING SAP
"Site Logistics Request" business object that neither this custom
OData service nor any other SAP_SOAP_*/BYD_ODATA_* endpoint already
configured in this app exposes a way to create or discover. This
matches SAP Community's own explicit answer (checked live, Aug 27 2026)
that genuinely combining several ScheduleLines into one Delivery via a
stateless external OData caller is only supported through ABSL's own
`.RequestDeliveryExecution()` call on a native collection (which
internally has access to create/attach that Site Logistics Request) -
ruled out per the user's explicit no-custom-SDK constraint.

Attempt #7 (Aug 27 2026, same session): `SLPGIInBackground`, a
schedule-line-scoped (not item-scoped) sibling of `PGIInBackground`
with a much simpler single-`ObjectID` signature (no Task/Split/
AutoRelease controls at all) - tried next since it operates at the
finer ScheduleLine granularity SAP's own internal due-list batching
naturally works on. CONFIRMED LIVE DEAD END (Aug 27 2026, order 30413):
succeeded with NO error for all 3 lines, but still produced 3 separate
Delivery documents (P9D1-4174/4175/4176) - same outcome as the
item-level PGIInBackground loop, just via a different action. Every
standard OData/SOAP action discoverable in this tenant's granted
services ($metadata fully enumerated) has now been tried; combining
requires either ABSL (ruled out) or a pre-existing "Site Logistics
Request" object this app has no service to create (attempt #6). Kept
as a best-effort pre-step ahead of the always-safe per-line loop
below; any failure here is caught/logged and changes nothing about the
existing, proven fallback."""
import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

REFERENCE_ENTITY_SET = "OutboundDeliveryRequestItemBusinessTransactionDocumentReferenceSalesOrderCollect"
STOCK_TRANSFER_ORDER_TYPE_CODE = "814"  # confirmed live via this tenant's own code list


class SAPOutboundDeliveryError(Exception):
    pass


class SAPOutboundDeliveryClient:
    def __init__(self, endpoint: str, username: str, password: str, vhost: str):
        self.endpoint = endpoint.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.vhost = vhost

    def find_delivery_request_items(self, customer_requirement_uuid: str) -> list:
        """Returns a LIST of {"object_id" (item-level), "parent_object_id"
        (the ONE Outbound Delivery Request DOCUMENT this line belongs to
        - shared across every line SAP put on the same document), "uuid"
        (this line's own ConfirmationItemUUID, needed afterwards to look
        up the resulting Outbound Delivery's human-readable ID via
        find_outbound_delivery_ids), "order_fulfilment_status",
        "product_id", "description"} - one per Outbound Delivery Request
        ITEM SAP produced from our Customer Requirement (a multi-line STO
        produces one row per line here, all sharing the same header
        UUID), or [] if SAP hasn't converted it yet - matched on UUID
        alone, see module docstring.

        Aug 27 2026 bug fix: this used to be find_delivery_request_item
        (singular) and `return`ed on the FIRST row only - fine for a
        single-line STO, but for a multi-line one it silently posted
        Goods Issue for just ONE line's Outbound Delivery Request Item
        and never even looked at the rest, so SAP created a real
        Outbound Delivery containing only that one line while the other
        lines stayed stuck, un-posted, forever (real incident: STO-000011
        / SAP order 30280, 4 lines, only line 1/HRPIPE3329 ever left the
        source site)."""
        url = f"{self.endpoint}/{REFERENCE_ENTITY_SET}"
        params = {
            "$filter": f"UUID eq guid'{customer_requirement_uuid.upper()}'",
            "$expand": "OutboundDeliveryRequestItem/OutboundDeliveryRequestItemScheduleLine",
            "$format": "json",
            "sap-vhost": self.vhost,
        }
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        results = []
        for row in resp.json().get("d", {}).get("results", []):
            item = row.get("OutboundDeliveryRequestItem")
            if not isinstance(item, dict) or "__deferred" in item:
                continue
            schedule_lines = item.get("OutboundDeliveryRequestItemScheduleLine")
            schedule_line_object_id = None
            if isinstance(schedule_lines, dict):
                schedule_lines = schedule_lines.get("results") or []
            if isinstance(schedule_lines, list) and schedule_lines:
                schedule_line_object_id = schedule_lines[0].get("ObjectID")
            results.append({
                "object_id": item.get("ObjectID"),
                "parent_object_id": item.get("ParentObjectID"),
                "uuid": item.get("UUID"),
                "order_fulfilment_status": item.get("OrderFulfilmentProcessingStatusCode"),
                "product_id": item.get("RayItemcode_KUT"),
                "description": item.get("RAYITEMDESCRIPTION_KUT"),
                "schedule_line_object_id": schedule_line_object_id,
            })
        return results

    def get_delivery_request_display_id(self, parent_object_id: str) -> str:
        """The human-readable Delivery Request ID (e.g. "58918", what the
        SAP UI's Delivery Proposals screen itself calls "Delivery Request
        ID") - a header-level field, NOT present on the item-level rows
        `find_delivery_request_items` returns above (their own "ID" field
        is always blank; live-verified). User's explicit ask (Aug 31
        2026) to surface this in our own UI as soon as it exists, instead
        of only being visible by opening the SAP screen directly."""
        url = f"{self.endpoint}/OutboundDeliveryRequestCollection"
        params = {
            "$filter": f"ObjectID eq '{parent_object_id}'",
            "$format": "json",
            "sap-vhost": self.vhost,
        }
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("d", {}).get("results", [])
        return results[0].get("BaseBusinessTransactionDocumentID") if results else None

    def find_outbound_delivery_ids(self, item_uuids: list) -> list:
        """Aug 27 2026, user's explicit ask ("please visible delivery id
        also") - once a line's Outbound Delivery Request Item has
        actually been turned into a real Outbound Delivery (via
        post_goods_issue), looks up that delivery's own human-readable ID
        (e.g. "P8D1-185") via
        OutboundDeliveryItemBusinessTransactionDocumentReferenceOutboundDeliveryRequestC,
        filtered on `ItemUUID` (confirmed live - NOT the collection's own
        `UUID` field, which is unrelated here) against each of our line's
        `uuid` (its ConfirmationItemUUID from find_delivery_request_items),
        expanding the `OutboundDelivery` nav property for its `ID`.
        Returns a de-duplicated list of Delivery IDs (empty if none of
        the given items has been delivered yet)."""
        item_uuids = [u for u in item_uuids if u]
        if not item_uuids:
            return []
        url = f"{self.endpoint}/OutboundDeliveryItemBusinessTransactionDocumentReferenceOutboundDeliveryRequestC"
        filter_clause = " or ".join(f"ItemUUID eq guid'{u.upper()}'" for u in item_uuids)
        params = {"$filter": filter_clause, "$expand": "OutboundDelivery", "$format": "json", "sap-vhost": self.vhost}
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        ids = []
        for row in resp.json().get("d", {}).get("results", []):
            delivery = row.get("OutboundDelivery")
            if isinstance(delivery, dict) and delivery.get("ID") and delivery["ID"] not in ids:
                ids.append(delivery["ID"])
        return ids

    def find_outbound_delivery_objects(self, item_uuids: list) -> list:
        """Same query/link as find_outbound_delivery_ids above, but also
        keeps each resulting Delivery's own `ObjectID` (needed to call
        release_outbound_delivery below - the human-readable `ID` alone
        can't be posted back to SAP) and which of our `item_uuids` it
        resolved from (`item_uuid`, straight off this link table's own
        `ItemUUID` column) - lets the caller check whether EVERY line it
        asked about has resolved to some Delivery yet, not just count how
        many rows came back."""
        item_uuids = [u for u in item_uuids if u]
        if not item_uuids:
            return []
        url = f"{self.endpoint}/OutboundDeliveryItemBusinessTransactionDocumentReferenceOutboundDeliveryRequestC"
        filter_clause = " or ".join(f"ItemUUID eq guid'{u.upper()}'" for u in item_uuids)
        params = {"$filter": filter_clause, "$expand": "OutboundDelivery", "$format": "json", "sap-vhost": self.vhost}
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        results = []
        for row in resp.json().get("d", {}).get("results", []):
            delivery = row.get("OutboundDelivery")
            if not isinstance(delivery, dict) or not delivery.get("ObjectID"):
                continue
            results.append({"object_id": delivery["ObjectID"], "id": delivery.get("ID"), "item_uuid": row.get("ItemUUID")})
        return results

    def get_delivery_object_id_by_id(self, delivery_id: str) -> str:
        """Real incident fix (Order 30518/Delivery P1D1-492, Sep 2026):
        looks up an Outbound Delivery's `ObjectID` (needed by
        release_outbound_delivery below) from just its human-readable
        `ID` (e.g. "P1D1-492") - used on a RETRY when this app already
        knows a Delivery exists (`outbound_delivery_ids` persisted on the
        STO doc) but its underlying request items are gone from SAP's
        pending list for good, so the normal item_uuid-based lookup
        (find_outbound_delivery_objects) can no longer be used."""
        url = f"{self.endpoint}/OutboundDeliveryCollection"
        params = {"$filter": f"ID eq '{delivery_id}'", "$format": "json", "sap-vhost": self.vhost}
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("d", {}).get("results", [])
        if not results or not results[0].get("ObjectID"):
            raise SAPOutboundDeliveryError(f"Delivery {delivery_id} not found via SAP OData")
        return results[0]["ObjectID"]

    def release_outbound_delivery(self, delivery_object_id: str) -> dict:
        """`OutboundDeliveryRelease` (confirmed via $metadata: bound to
        `OutboundDeliveryCollection`, single `ObjectID` param, same
        CSRF+POST pattern as post_goods_issue) - the explicit release
        step this file now always performs itself (Aug 27 2026, "one
        combined delivery" attempt #5 - see module docstring) instead of
        letting `PGIInBackground`'s own `AutoReleaseOutboundDelivery`
        flag do it inline per item."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, entity_set="OutboundDeliveryCollection")
                params = {"ObjectID": f"'{delivery_object_id}'", "sap-vhost": self.vhost}
                resp = session.post(
                    f"{self.endpoint}/OutboundDeliveryRelease",
                    params=params,
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body.get("d", body)

    def _fetch_csrf_token(self, session: requests.Session, entity_set: str = "OutboundDeliveryRequestCollection") -> str:
        resp = session.get(
            f"{self.endpoint}/{entity_set}",
            params={"$top": "1", "sap-vhost": self.vhost},
            auth=self.auth,
            headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
            timeout=30,
        )
        token = resp.headers.get("x-csrf-token")
        if not token:
            raise SAPOutboundDeliveryError("SAP did not return a CSRF token - cannot post Goods Issue.")
        return token

    def request_delivery_execution(self, schedule_line_object_id: str, target_uuid: str) -> dict:
        """SLRequestDeliveryExecution (Aug 27 2026, attempt #6 - see module
        docstring). Bound to `OutboundDeliveryRequestItemScheduleLineCollection`
        (schedule-line ObjectID, NOT the item-level one `post_goods_issue`
        uses), REQUIRES `TargetSiteLogisticsRequestUUID` - a shared anchor
        GUID the caller supplies, meant to tell SAP "these schedule lines
        belong together". CONFIRMED LIVE DEAD END (Aug 27 2026, order
        30421): with a syntactically-correct GUID supplied, SAP now
        rejects with a clean business fault "Site logistics request does
        not exist" - `TargetSiteLogisticsRequestUUID` must reference an
        ALREADY-EXISTING SAP "Site Logistics Request" object, and no
        service exposed to this app (checked every SAP_SOAP_*/BYD_ODATA_*
        endpoint in .env) can create or look one up. Kept only for
        `try_post_goods_issue`'s always-safe best-effort pattern - never
        actually succeeds today, always falls through to the proven
        per-line loop."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, entity_set="OutboundDeliveryRequestItemScheduleLineCollection")
                params = {
                    "ObjectID": f"'{schedule_line_object_id}'",
                    "TargetSiteLogisticsRequestUUID": f"guid'{target_uuid}'",
                    "TaskBasedIndicator": "false",
                    "AllowSplitIndicator": "false",
                    "SplitByShippingOrPickupDateTimeIndicator": "false",
                    "SplitByOrderIndicator": "false",
                    "SplitByDeliveryPriorityCodeIndicator": "false",
                    "sap-vhost": self.vhost,
                }
                resp = session.post(
                    f"{self.endpoint}/SLRequestDeliveryExecution",
                    params=params,
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body.get("d", body)

    def post_goods_issue_schedule_line(self, schedule_line_object_id: str) -> dict:
        """SLPGIInBackground (Aug 27 2026, attempt #7 - see module
        docstring) - a schedule-line-scoped sibling of `post_goods_issue`
        below, single `ObjectID` param only (no Task/Split/AutoRelease
        controls exposed on this action at all, confirmed via
        $metadata - always auto-releases). Tried because it operates at
        the finer ScheduleLine granularity SAP's own internal due-list
        batching naturally works on, unlike the item-level
        `PGIInBackground`."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, entity_set="OutboundDeliveryRequestItemScheduleLineCollection")
                params = {"ObjectID": f"'{schedule_line_object_id}'", "sap-vhost": self.vhost}
                resp = session.post(
                    f"{self.endpoint}/SLPGIInBackground",
                    params=params,
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body.get("d", body)

    def post_goods_issue(self, outbound_delivery_request_item_object_id: str, auto_release: bool = False) -> dict:
        """PGIInBackground - creates the Outbound Delivery and posts the
        Goods Issue (TaskBasedIndicator=false, this tenant's own proven
        usage - no separate Warehouse Task confirmation needed).

        `auto_release` (Aug 27 2026, "one combined delivery" attempt #5 -
        see module docstring) now defaults to False: the resulting
        Delivery is left unreleased here, and stock_transfer_service.py's
        `_release_ready_deliveries` explicitly releases it afterwards
        (via release_outbound_delivery below) once it's checked what
        every line in the same STO actually resolved to - was always
        `true` (inline, one release per item, right here) before this
        change."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session)
                params = {
                    "ObjectID": f"'{outbound_delivery_request_item_object_id}'",
                    "TaskBasedIndicator": "false",
                    "AllowSplitIndicator": "false",
                    "AutoReleaseOutboundDelivery": "true" if auto_release else "false",
                    "SplitByDeliveryPriorityCodeIndicator": "false",
                    "SplitByOrderIndicator": "false",
                    "SplitByShippingOrPickupDateTimeIndicator": "false",
                    "sap-vhost": self.vhost,
                }
                resp = session.post(
                    f"{self.endpoint}/PGIInBackground",
                    params=params,
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body.get("d", body)
