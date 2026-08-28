"""SAP Business ByDesign custom OData service `inboundstockemergent` -
posts the Goods Receipt for an Inbound Delivery Notification (Aug 28
2026, the "Inbound STO Receipt" feature - see stock_transfer_service.py
module docstring for the outbound side this mirrors).

Discovered live this session:
  - SAP has NO web service/API to create a "Confirmed Inbound Delivery"
    (the doc that carries actual received quantities) directly -
    confirmed via SAP's own KBA 3583076. The only supported path is the
    `InboundDelivery` (UI: "Inbound Delivery Notification") BO's own
    `PGR Ground`/`PGRBackground` action - exposed here as the Function
    Import `InboundDeliveryPGRBackground` - which posts the Goods
    Receipt for whatever quantity is CURRENTLY on the Notification (no
    quantity override parameter exists on the action itself).
  - The Inbound Delivery Notification's own human-readable `ID` is
    IDENTICAL to the Outbound Delivery ID that produced it (confirmed
    live: STO-000046's outbound deliveries P8D1-185/186/187 each
    resolve 1:1 via `$filter=ID eq 'P8D1-185'` to their own Inbound
    Delivery Notification at the receiving site) - so no UUID-matching
    dance is needed here at all, unlike the outbound side.
  - To receive a DIFFERENT quantity than what's on the Notification,
    PATCH the Item's own `InboundDeliveryItemQuantityCollection` row
    BEFORE calling PGRBackground - the action itself has no quantity
    param (confirmed via $metadata + SAP KBA 3583076), so adjusting the
    source row first is the only lever this OData service exposes.

Auth: same Business User as the outbound service
(SAP_ODATA_USERNAME/PASSWORD) - confirmed live to have read access on
this service; PGRBackground/Release write access confirmed live via a
dedicated Work Center View grant ("Inbound Delivery Notifications",
under Inbound Logistics) on the `_EMERGENTBOM` technical/Business
Configuration path used elsewhere in this app."""
import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore


class SAPInboundDeliveryError(Exception):
    pass


class SAPInboundDeliveryClient:
    def __init__(self, endpoint: str, username: str, password: str, vhost: str):
        self.endpoint = endpoint.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.vhost = vhost

    def find_delivery_by_id(self, delivery_id: str) -> dict:
        """Looks up the Inbound Delivery Notification whose own `ID`
        matches the given (Outbound Delivery) ID - e.g. 'P8D1-185'.
        Returns {object_id, uuid, id, items: [{item_object_id,
        product_id, quantity, unit_code, quantity_object_id}]}, or None
        if SAP hasn't created it yet."""
        url = f"{self.endpoint}/InboundDeliveryCollection"
        params = {
            "$filter": f"ID eq '{delivery_id}'",
            "$expand": "InboundDeliveryItem/InboundDeliveryItemQuantity",
            "$format": "json",
            "sap-vhost": self.vhost,
        }
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPInboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("d", {}).get("results", [])
        if not results:
            return None
        row = results[0]
        items = row.get("InboundDeliveryItem")
        if isinstance(items, dict):
            items = items.get("results") or []
        parsed_items = []
        for it in (items or []):
            quantities = it.get("InboundDeliveryItemQuantity")
            if isinstance(quantities, dict):
                quantities = quantities.get("results") or []
            qty_row = quantities[0] if quantities else {}
            parsed_items.append({
                "item_object_id": it.get("ObjectID"),
                "product_id": it.get("ProductID"),
                "quantity": float(qty_row.get("Quantity") or 0),
                "unit_code": qty_row.get("unitCode"),
                "quantity_object_id": qty_row.get("ObjectID"),
            })
        return {"object_id": row.get("ObjectID"), "uuid": row.get("UUID"), "id": row.get("ID"), "items": parsed_items}

    def update_item_quantity(self, quantity_object_id: str, new_quantity: float) -> None:
        """PATCH an Item's quantity row to something other than what SAP
        put there by default - must happen BEFORE post_goods_receipt,
        since PGRBackground itself has no quantity override (see module
        docstring)."""
        session = requests.Session()
        entity_set = "InboundDeliveryItemQuantityCollection"
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, entity_set)
                resp = session.patch(
                    f"{self.endpoint}/{entity_set}('{quantity_object_id}')",
                    params={"sap-vhost": self.vhost},
                    json={"Quantity": f"{new_quantity:.2f}"},
                    auth=self.auth,
                    headers={"Accept": "application/json", "Content-Type": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPInboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")

    def post_goods_receipt(self, delivery_object_id: str) -> dict:
        """InboundDeliveryPGRBackground (Function Import, see module
        docstring) - posts the Goods Receipt for every item currently on
        this Inbound Delivery Notification."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, "InboundDeliveryCollection")
                resp = session.post(
                    f"{self.endpoint}/InboundDeliveryPGRBackground",
                    params={"ObjectID": f"'{delivery_object_id}'", "sap-vhost": self.vhost},
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPInboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body.get("d", body)

    def _fetch_csrf_token(self, session: requests.Session, entity_set: str) -> str:
        resp = session.get(
            f"{self.endpoint}/{entity_set}",
            params={"$top": "1", "sap-vhost": self.vhost},
            auth=self.auth,
            headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
            timeout=30,
        )
        token = resp.headers.get("x-csrf-token")
        if not token:
            raise SAPInboundDeliveryError("SAP did not return a CSRF token - cannot post Goods Receipt.")
        return token
