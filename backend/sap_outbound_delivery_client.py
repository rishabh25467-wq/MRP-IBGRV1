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

Aug 27 2026, 2nd real incident (STO-000046, SAP order 30336, 3 lines):
posting Goods Issue once PER LINE's own item-level ObjectID (the initial
fix for the first incident above) does stop lines from being silently
skipped, but it makes SAP create ONE SEPARATE Outbound Delivery per line
(e.g. P8D1-185/186/187) instead of the single combined delivery the user
expects for one Stock Transfer Order. Confirmed live: every
OutboundDeliveryRequestItem row for the SAME Customer Requirement shares
one common `ParentObjectID` - THAT is the actual Outbound Delivery
Request DOCUMENT's own ObjectID. PGIInBackground must be called ONCE per
DOCUMENT (i.e. once per distinct ParentObjectID, not once per item) so
every one of its lines is released together into a single Outbound
Delivery - see stock_transfer_service.py's try_post_goods_issue, which
now groups pending lines by `parent_object_id` before posting."""
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
            "$expand": "OutboundDeliveryRequestItem",
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
            results.append({
                "object_id": item.get("ObjectID"),
                "parent_object_id": item.get("ParentObjectID"),
                "uuid": item.get("UUID"),
                "order_fulfilment_status": item.get("OrderFulfilmentProcessingStatusCode"),
                "product_id": item.get("RayItemcode_KUT"),
                "description": item.get("RAYITEMDESCRIPTION_KUT"),
            })
        return results

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

    def post_goods_issue(self, outbound_delivery_request_item_object_id: str) -> dict:
        """PGIInBackground - creates the Outbound Delivery, posts the
        Goods Issue, and releases it in ONE call (TaskBasedIndicator=false
        + AutoReleaseOutboundDelivery=true, this tenant's own proven
        usage - no separate Warehouse Task confirmation needed)."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session)
                params = {
                    "ObjectID": f"'{outbound_delivery_request_item_object_id}'",
                    "TaskBasedIndicator": "false",
                    "AllowSplitIndicator": "false",
                    "AutoReleaseOutboundDelivery": "true",
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
