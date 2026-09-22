"""SAP Business ByDesign custom OData service `inboundstockemergent` -
posts the Goods Receipt for an Inbound Delivery Notification (Aug 28
2026, the "Inbound STO Receipt" feature - see stock_transfer_service.py
module docstring for the outbound side this mirrors).

Sep 22 2026 update: this class is also instantiated a second time in
server.py (as `sap_kh_inbound_delivery_client`) pointed at the
`khinbounddelivery` service instead (same underlying SAP object,
confirmed live - `ID`/`ObjectID` are identical across both services).
That second instance is the one used for `acknowledge_delivery_note_
receipt`/`release_delivery` in the new automated GR flow (see
inbound_receipt_service.start_automated_receipt) - `inboundstockemergent`
never exposed `AcknowledgeDeliveryNoteReceipt` at all, and
`PGRBackground`/direct GR posting is confirmed dead below, so this file's
own `endpoint` (inboundstockemergent) is only used for `find_delivery_by_id`
in production today.

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
    def __init__(self, endpoint: str, username: str, password: str, vhost: str, release_action: str = "InboundDeliveryRelease"):
        self.endpoint = endpoint.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.vhost = vhost
        # `khinbounddelivery` names this Function Import just "Release"
        # (confirmed live, Sep 22 2026) - `inboundstockemergent` (this
        # class's original service) uses "InboundDeliveryRelease".
        self.release_action = release_action

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
        return {
            "object_id": row.get("ObjectID"), "uuid": row.get("UUID"), "id": row.get("ID"), "items": parsed_items,
            "release_status_code": row.get("ReleaseStatusCode"),
            "delivery_processing_status_code": row.get("DeliveryProcessingStatusCode"),
            "delivery_note_status_code": row.get("DeliveryNoteStatusCode"),
        }

    def acknowledge_delivery_note_receipt(self, delivery_object_id: str) -> None:
        """AcknowledgeDeliveryNoteReceipt (Function Import) - flips the
        Notification from "Advised" to "Received" (confirmed live, Sep 22
        2026). Discovered as a required prerequisite: `Release` itself
        errors with "action is disabled" until this has run first. Only
        exposed on the `khinbounddelivery` service (not
        `inboundstockemergent`) - see module docstring. A 4xx here
        usually just means the Notification was already acknowledged by
        an earlier attempt; callers treat it as non-fatal."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, "InboundDeliveryCollection")
                resp = session.post(
                    f"{self.endpoint}/AcknowledgeDeliveryNoteReceipt",
                    params={"ObjectID": f"'{delivery_object_id}'", "sap-vhost": self.vhost},
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPInboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")

    def find_object_id(self, delivery_id: str) -> dict:
        """Lightweight lookup (no $expand) - just the ObjectID + status
        codes, not line items. Needed because `khinbounddelivery` (used
        for Acknowledge/Release - see server.py's
        sap_kh_inbound_delivery_client) exposes a different item
        navigation property name than `inboundstockemergent`
        (confirmed live, Sep 22 2026: `find_delivery_by_id`'s
        `InboundDeliveryItem` expand 404s there) - this avoids relying
        on item-schema compatibility between the two services at all."""
        url = f"{self.endpoint}/InboundDeliveryCollection"
        params = {"$filter": f"ID eq '{delivery_id}'", "$format": "json", "sap-vhost": self.vhost}
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
        return {
            "object_id": row.get("ObjectID"), "id": row.get("ID"),
            "release_status_code": row.get("ReleaseStatusCode"),
            "delivery_processing_status_code": row.get("DeliveryProcessingStatusCode"),
            "delivery_note_status_code": row.get("DeliveryNoteStatusCode"),
        }

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

    def release_delivery(self, delivery_object_id: str) -> None:
        """InboundDeliveryRelease (Function Import) - PGRBackground errors
        with "action is disabled" until the Notification has been
        Released first (confirmed live, Aug 28 2026 - SAP's own action
        model gates PGRBackground on release status, same as the
        outbound side's Delivery needing an explicit release before its
        own PGI). Must call `acknowledge_delivery_note_receipt` first.

        Sep 22 2026 update: `TaskBasedIndicator=true` now (was `false`) -
        live testing this session proved site P1 always spawns the real
        Warehouse Request/Warehouse Order chain regardless of this flag
        (it's driven by the site's own Material Flow config, not this
        param), so `true` (forward to warehouse execution, the normal SAP
        behavior) is the correct/tested value - see
        inbound_receipt_service.start_automated_receipt for what happens
        next depending on whether a Warehouse Order actually gets created."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, "InboundDeliveryCollection")
                resp = session.post(
                    f"{self.endpoint}/{self.release_action}",
                    params={"ObjectID": f"'{delivery_object_id}'", "TaskBasedIndicator": "true", "sap-vhost": self.vhost},
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPInboundDeliveryError(f"HTTP {resp.status_code}: {resp.text[:500]}")

    def post_goods_receipt(self, delivery_object_id: str) -> dict:
        """InboundDeliveryPGRBackground (Function Import, see module
        docstring) - posts the Goods Receipt for every item currently on
        this Inbound Delivery Notification. Must be Released first (see
        release_delivery) or SAP returns "action is disabled"."""
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
