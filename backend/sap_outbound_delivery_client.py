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

GST / E-way bill fields (Aug 2026 session) - real write path FOUND (not a
guess): this SAME `odataoutboundemergent` service's `$metadata` already
carries the exact 5 custom PDI fields shown on SAP's "Stock Transfer
Delivery" screen, on the `OutboundDeliveryRequestCollection` (the ODR
HEADER, not the item) - `TransportationMode_KUT` (String, picklist-backed
but free-text-writable, confirmed live values "By Road"/"By Rail"/
"By Air"/"By Self" - NOT "By Sea", frontend dropdown aligned to match),
`VehicleNo_KUT`, `PlaceOfSupply_KUT`, `GRNo1_KUT` (note the "1" - this
tenant's own field name, not a typo), `DateOfSupply_KUT` (Edm.DateTime,
OData v2 JSON `/Date(ms)/` wire format) - all `sap:creatable="true"
sap:updatable="true"`. Confirmed live: writable ONLY while the ODR is
still open (`OrderFulfilmentProcessingStatusCode` != "3"/Finished) - a
real MERGE attempt on an already-GI'd ODR came back
"Changing data not possible; data is read-only". So this MUST be pushed
BEFORE post_goods_issue() converts/closes the request, not after - wired
into stock_transfer_service.try_post_goods_issue() in that exact order."""
import json
import requests
from datetime import date, datetime, timezone
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

REFERENCE_ENTITY_SET = "OutboundDeliveryRequestItemBusinessTransactionDocumentReferenceSalesOrderCollect"
STOCK_TRANSFER_ORDER_TYPE_CODE = "814"  # confirmed live via this tenant's own code list

# our field -> confirmed real SAP technical field name on OutboundDeliveryRequestCollection
GST_FIELD_MAP = {
    "transportation_mode": "TransportationMode_KUT",
    "vehicle_no": "VehicleNo_KUT",
    "place_of_supply": "PlaceOfSupply_KUT",
    "gr_no": "GRNo1_KUT",
}
GST_DATE_FIELD_MAP = {"date_of_supply": "DateOfSupply_KUT"}


class SAPOutboundDeliveryError(Exception):
    pass


class SAPOutboundDeliveryClient:
    def __init__(self, endpoint: str, username: str, password: str, vhost: str):
        self.endpoint = endpoint.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.vhost = vhost

    def find_delivery_request_item(self, customer_requirement_uuid: str):
        """Returns {"object_id", "order_fulfilment_status", "product_id",
        "description"} for the Outbound Delivery Request Item SAP
        produced from our Customer Requirement, or None if SAP hasn't
        converted it yet - matched on UUID alone, see module docstring."""
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
        for row in resp.json().get("d", {}).get("results", []):
            item = row.get("OutboundDeliveryRequestItem")
            if not isinstance(item, dict) or "__deferred" in item:
                continue
            return {
                "object_id": item.get("ObjectID"),
                "parent_object_id": item.get("ParentObjectID"),
                "order_fulfilment_status": item.get("OrderFulfilmentProcessingStatusCode"),
                "product_id": item.get("RayItemcode_KUT"),
                "description": item.get("RAYITEMDESCRIPTION_KUT"),
            }
        return None

    def push_gst_fields(self, outbound_delivery_request_object_id: str, gst_fields: dict) -> dict:
        """MERGE the 5 confirmed GST/e-way-bill custom fields onto the ODR
        HEADER (see module docstring) - MUST be called before
        post_goods_issue() on the same order, while the ODR is still open.
        `gst_fields` uses OUR keys (transportation_mode/vehicle_no/
        place_of_supply/gr_no/date_of_supply) - unknown/blank keys are
        skipped."""
        body = {}
        for our_key, sap_field in GST_FIELD_MAP.items():
            value = gst_fields.get(our_key)
            if value:
                body[sap_field] = value
        for our_key, sap_field in GST_DATE_FIELD_MAP.items():
            value = gst_fields.get(our_key)
            if value:
                d = date.fromisoformat(value)
                epoch_ms = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
                body[sap_field] = f"/Date({epoch_ms})/"
        if not body:
            return {"pushed": False, "reason": "no GST fields on this order"}

        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, entity_set="OutboundDeliveryItemCollection")
                resp = session.post(
                    f"{self.endpoint}/OutboundDeliveryRequestCollection('{outbound_delivery_request_object_id}')",
                    params={"sap-vhost": self.vhost},
                    auth=self.auth,
                    headers={"Accept": "application/json", "Content-Type": "application/json",
                             "X-CSRF-Token": token, "X-HTTP-Method": "MERGE"},
                    data=json.dumps(body),
                    timeout=30,
                )
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPOutboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return {"pushed": True}

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
