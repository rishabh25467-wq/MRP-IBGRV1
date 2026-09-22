# STO "Receive" Button — Full Migration Bundle

Everything needed to port the Inbound STO Receipt flow ("Receive" button) into a new app.
This is the fully synchronous flow (no Playwright, no polling/webhooks) built Sep 2026:

```
Acknowledge -> PostGoodsReceipt (Path A, primary)
   -> if rejected: Release -> immediate Warehouse-Order lookup -> ConfirmAsPlanned (Path B, fallback)
-> Goods Movement: {SITE}-HOLD -> real target warehouse (relocation)
```

## 1. Required `.env` variables (backend)

Copy these keys into the new app's `backend/.env`. Values below are the REAL endpoint URLs (not
secrets — safe to copy as-is if pointing at the SAME SAP tenant). Replace `SAP_SOAP_PASSWORD` /
`SAP_ODATA_PASSWORD` with your own values (you said the new app already has credentials — just
make sure the variable NAMES below match, or adjust the client instantiation code in section 3).

```env
BYD_ODATA_VHOST="my431827.businessbydesign.cloud.sap"

# OData - Inbound Delivery Notification (find_delivery_by_id / PGR fallback lookups)
SAP_ODATA_INBOUND_BASE_URL="https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/inboundstockemergent"

# OData - Inbound Delivery, "kh" custom service (Acknowledge / Release — Path A + B)
SAP_ODATA_KH_INBOUND_DELIVERY_BASE_URL="https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/khinbounddelivery"

# OData - Warehouse Order / Site Logistics Lot lookup + ConfirmAsPlanned (Path B fallback only)
SAP_ODATA_INBOUND_DELIVERY_EXECUTION_BASE_URL="https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/khinbounddeliveryexecution"

# OData Business User (used by all 3 clients above)
SAP_ODATA_USERNAME="UNEECOPSTEAM"
SAP_ODATA_PASSWORD="<your value>"

# SOAP - Goods Movement (the {SITE}-HOLD -> real target warehouse relocation step)
SAP_SOAP_GOODS_MOVEMENT_ENDPOINT="https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/inventoryprocessinggoodsandac2?sap-vhost=my431827.businessbydesign.cloud.sap"
SAP_SOAP_USERNAME="_EMERGENTBOM"
SAP_SOAP_PASSWORD="<your value>"

# Optional - flips the Goods Movement SOAP call from dry-run to a real write.
# MUST be "false" for the relocation step to actually move stock in SAP.
SAP_GOODS_MOVEMENT_DRY_RUN="false"
```

No other secrets are needed for this specific flow (STO creation / Goods Issue / Outbound
Delivery are a SEPARATE feature with their own endpoints — not included here since the ask was
specifically the Receive button).

## 2. Backend files — copy verbatim

### `sap_rate_limiter.py` (new file)
Shared concurrency cap — every SAP call in this flow depends on it.

```python
"""Global concurrency limiter shared by every SAP client (SOAP + OData).
SAP Business ByDesign tenants enforce a small concurrent web-service session limit per tenant.
Every direct requests.get/post call to the SAP host should be wrapped in `with sap_semaphore:`."""
import threading

SAP_MAX_CONCURRENT_REQUESTS = 3

sap_semaphore = threading.Semaphore(SAP_MAX_CONCURRENT_REQUESTS)
```

### `sap_wip_clearing_client.py` (new file — only the 2 functions this flow needs)
Resolves which SAP Company/Set-of-Books code a site posts under, and which warehouse a site's
Goods Receipt actually lands stock into before relocation. **Edit `SITE_TO_COMPANY` and
`SITE_INBOUND_STAGING_AREA_OVERRIDE` for your own site list** — these are tenant-specific business
config, not derivable from any API.

```python
SITE_TO_COMPANY = {
    "P1": ("RI", "RSOB"),
    "P8": ("RI", "RSOB"),
    "P5": ("RI", "RSOB"),
    "P1W": ("RI", "RSOB"),
    "W1": ("RI", "RSOB"),
}
DEFAULT_COMPANY = ("RT", "RDOB")


def company_and_set_of_books_for_site(site_id: str):
    return SITE_TO_COMPANY.get((site_id or "").strip().upper(), DEFAULT_COMPANY)


# Per-site override for where Goods Receipt actually lands (defaults to "{SITE}-HOLD").
SITE_INBOUND_STAGING_AREA_OVERRIDE = {"P3": "P3-Z1-01-A"}


def inbound_staging_area_for_site(site_id: str) -> str:
    site_id = (site_id or "").strip().upper()
    return SITE_INBOUND_STAGING_AREA_OVERRIDE.get(site_id) or f"{site_id}-HOLD"
```

### `sap_inbound_delivery_client.py` (new file, full)
Same class instantiated TWICE in server.py (section 4) — once per OData service. Handles
Acknowledge / Release / PostGoodsReceipt / delivery lookup / quantity override.

```python
"""SAP Business ByDesign custom OData service for Inbound Delivery Notifications.
Discovered live:
  - SAP has NO web service to create a "Confirmed Inbound Delivery" directly (KBA 3583076).
    The only supported path is InboundDelivery's own PGRBackground action, exposed here as
    InboundDeliveryPGRBackground - posts whatever quantity is CURRENTLY on the Notification.
  - The Notification's own `ID` is IDENTICAL to the Outbound Delivery ID that produced it.
  - To receive a different quantity, PATCH InboundDeliveryItemQuantityCollection BEFORE PGR."""
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
        # `khinbounddelivery` names this Function Import just "Release";
        # the plain "inboundstockemergent" service uses "InboundDeliveryRelease".
        self.release_action = release_action

    def find_delivery_by_id(self, delivery_id: str) -> dict:
        """Looks up the Inbound Delivery Notification whose own `ID` matches the given
        (Outbound Delivery) ID - e.g. 'P8D1-185'. Returns None if SAP hasn't created it yet."""
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
        """AcknowledgeDeliveryNoteReceipt (Function Import) - flips the Notification from
        "Advised" to "Received". Required prerequisite: Release errors with "action is
        disabled" until this has run first. Only exposed on the `khinbounddelivery` service."""
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
        """Lightweight lookup (no $expand) - just ObjectID + status codes."""
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
        """PATCH an Item's quantity row - must happen BEFORE post_goods_receipt (PGRBackground
        itself has no quantity override parameter)."""
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
        """InboundDeliveryRelease/Release (Function Import) - must call
        acknowledge_delivery_note_receipt first. TaskBasedIndicator=true forwards to warehouse
        execution (the correct/tested value - it's the site's own Material Flow config that
        actually decides whether a real task chain spawns, not this flag)."""
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
        """InboundDeliveryPGRBackground (Function Import) - posts the Goods Receipt for every
        item currently on this Notification. Must be Released first, or SAP returns
        "action is disabled". THIS IS PATH A's FINAL STEP when called WITHOUT Release first
        (see inbound_receipt_service.start_automated_receipt for why - calling Release BEFORE
        this disables it for this tenant; only call Release as the Path B FALLBACK)."""
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
```

### `sap_inbound_delivery_execution_client.py` (new file, full)
Path B fallback ONLY (task-based sites) — finds the Warehouse Order SAP spawned and confirms it.

```python
"""SAP Business ByDesign custom OData service for Warehouse Order / Site Logistics Lot lookup.
Only used as the Path B FALLBACK when direct PostGoodsReceipt is rejected by SAP (e.g. a
delivery that's already Released-but-unfinished from an earlier attempt)."""
import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore


class SAPInboundDeliveryExecutionError(Exception):
    pass


class SAPInboundDeliveryExecutionClient:
    def __init__(self, endpoint: str, username: str, password: str, vhost: str):
        self.endpoint = endpoint.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.vhost = vhost

    _EXPAND = "SiteLogisticsLotMaterialOutput,SiteLogisticsLotOperation/SiteLogisticsLotOperationActivity"

    def find_recent_lots(self, limit: int = 50) -> list:
        """A SINGLE, immediate, un-retried lookup at Warehouse Orders SAP has created - no
        polling. No $orderby (this custom OData service doesn't expose SystemAdministrativeData
        for sorting) - relies on SAP's own default order + a generous $top. Returns [] if SAP
        hasn't created it yet - caller treats that as an immediate failure, not something to
        wait/retry for."""
        url = f"{self.endpoint}/SiteLogisticsLotSiteLogisticLotCollection"
        params = {"$top": str(limit), "$expand": self._EXPAND, "$format": "json", "sap-vhost": self.vhost}
        return [self._parse_lot_row(row) for row in self._query(url, params)]

    def _query(self, url: str, params: dict) -> list:
        try:
            with sap_semaphore:
                resp = requests.get(url, params=params, auth=self.auth, headers={"Accept": "application/json"}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryExecutionError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPInboundDeliveryExecutionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("d", {}).get("results", [])

    def _parse_lot_row(self, row: dict) -> dict:
        outputs = self._as_list(row.get("SiteLogisticsLotMaterialOutput"))
        products = sorted({o.get("ProductID") for o in outputs if o.get("ProductID")})
        site_ids = {o.get("SiteID") for o in outputs if o.get("SiteID")}
        site_id = next(iter(site_ids), None)
        activities = []
        for op in self._as_list(row.get("SiteLogisticsLotOperation")):
            for act in self._as_list(op.get("SiteLogisticsLotOperationActivity")):
                activities.append({"object_id": act.get("ObjectID"), "status_code": act.get("ActivityProcessingStatusCode")})
        return {
            "object_id": row.get("ObjectID"), "id": row.get("ID"),
            "life_cycle_status_code": row.get("LifeCycleStatusCode"),
            "site_id": site_id, "products": products, "activities": activities,
        }

    def confirm_activity_as_planned(self, activity_object_id: str) -> None:
        """SiteLogisticsLotOperationActivityConfirmAsPlanned - confirms the Put Away task at its
        full planned quantity, which is what actually posts the Goods Receipt for this path."""
        session = requests.Session()
        try:
            with sap_semaphore:
                token = self._fetch_csrf_token(session, "SiteLogisticsLotOperationActivityCollection")
                resp = session.post(
                    f"{self.endpoint}/SiteLogisticsLotOperationActivityConfirmAsPlanned",
                    params={"ObjectID": f"'{activity_object_id}'", "sap-vhost": self.vhost},
                    auth=self.auth,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryExecutionError(f"Could not reach SAP: {e}")
        if resp.status_code >= 400:
            raise SAPInboundDeliveryExecutionError(f"HTTP {resp.status_code}: {resp.text[:500]}")

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
            raise SAPInboundDeliveryExecutionError("SAP did not return a CSRF token - cannot confirm the Put Away task.")
        return token

    @staticmethod
    def _as_list(value) -> list:
        if isinstance(value, dict):
            return value.get("results") or []
        return value or []
```

### `sap_goods_movement_client.py` (new file, full)
The final relocation step ({SITE}-HOLD → real target warehouse), via SOAP.
Includes the Sep 22 2026 `requests.Session` connection-pooling perf fix.

```python
"""Direct SAP Business ByDesign Goods Movement client - calls SAP's own
InventoryProcessingGoodsAndActivityConfirmationGoodsMovementIn.DoGoodsMovement SOAP service."""
import logging
import re
import uuid
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

SOAP_ACTION = "http://sap.com/xi/AP/LogisticsExecution/Global/InventoryProcessingGoodsAndActivityConfirmationGoodsMovementIn/DoGoodsMovement"


class SAPGoodsMovementError(Exception):
    pass


# SAP's <InventoryItemChangeQuantity unitCode> expects a real UN/CEFACT Rec 20 code (max 3
# chars, e.g. "KGM") - separate from QuantityTypeCode (a dimension label like "MASS"/"EA").
_UNIT_CODE_MAP = {"MASS": "KGM"}


def _resolve_unit_code(quantity_type_code: str) -> str:
    mapped = _UNIT_CODE_MAP.get(quantity_type_code)
    if mapped:
        return mapped
    if len(quantity_type_code) > 3:
        logger.warning(f"No _UNIT_CODE_MAP entry for '{quantity_type_code}' - truncating to 3 chars.")
    return quantity_type_code[:3]


def _build_envelope(external_id, external_item_id, site_id, product_id, owner_party_id,
                     source_area, target_area, quantity, unit_code, quantity_type_code, transaction_dt,
                     target_stock_status_code="", target_restricted_use=False) -> str:
    from xml.sax.saxutils import escape
    site_id, product_id, owner_party_id = escape(site_id), escape(product_id), escape(owner_party_id)
    source_area, target_area = escape(source_area), escape(target_area)
    quantity_str = format(quantity, "f").rstrip("0").rstrip(".") or "0"
    restricted_str = "true" if target_restricted_use else "false"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
  <soapenv:Body>
    <glob:GoodsAndActivityConfirmationGoodsMovement>
      <GoodsAndActivityConfirmation>
        <ExternalID>{external_id}</ExternalID>
        <SiteID>{site_id}</SiteID>
        <TransactionDateTime>{transaction_dt}</TransactionDateTime>
        <InventoryChangeItemGoodsMovement>
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
        </InventoryChangeItemGoodsMovement>
      </GoodsAndActivityConfirmation>
    </glob:GoodsAndActivityConfirmationGoodsMovement>
  </soapenv:Body>
</soapenv:Envelope>"""


def _extract_sap_error(xml: str):
    """SeverityCode 3+ is a real rejection even on an HTTP 200 response."""
    m = re.search(r"<SeverityCode>\s*[3-9]\s*</SeverityCode>", xml)
    if not m:
        return None
    note = re.search(r"<Note>(.*?)</Note>", xml)
    return note.group(1) if note else "SAP logged an error-severity item for this movement"


def _extract_gac_id(xml: str):
    """SAP's response echoes OUR OWN <ExternalGACID> alongside SAP's own real, permanent
    document number in <GACID> - always prefer GACID as the displayed reference."""
    m = re.search(r"<GACID>(.*?)</GACID>", xml)
    return m.group(1).strip() if m and m.group(1).strip() else None


# SAP's real Logistics Area ID is "{SITE}-{TYPE}" (e.g. "P2-RM"). Strip any "{site}/" composite
# display-key prefix some report data sources add (rejected as invalid by the write API).
def _normalize_logistics_area_id(value: str) -> str:
    return value.rsplit("/", 1)[-1]


class SAPGoodsMovementClient:
    def __init__(self, endpoint: str, username: str, password: str, timeout: tuple = (8, 30)):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout
        # Perf fix: reuse ONE requests.Session across every call (this client is a module-level
        # singleton) so concurrent relocation lines reuse pooled TCP/TLS connections instead of a
        # fresh handshake each time. requests.Session is thread-safe for concurrent use.
        self.session = requests.Session()

    def goods_movement(self, owner_party_id: str, product_id: str, source_logistics_area_id: str,
                        target_logistics_area_id: str, quantity: float, quantity_uom: str,
                        site_id: str, dry_run: bool = True, target_stock_status_code: str = "",
                        target_restricted_use: bool = False) -> dict:
        if quantity <= 0:
            raise SAPGoodsMovementError("quantity must be > 0")
        external_id = f"MOV-{uuid.uuid4().hex[:6].upper()}"
        external_item_id = f"I-{uuid.uuid4().hex[:8].upper()}"
        transaction_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        unit_code = _resolve_unit_code(quantity_uom)
        envelope = _build_envelope(
            external_id, external_item_id, site_id, product_id, owner_party_id,
            _normalize_logistics_area_id(source_logistics_area_id),
            _normalize_logistics_area_id(target_logistics_area_id),
            quantity, unit_code, quantity_uom, transaction_dt,
            target_stock_status_code=target_stock_status_code, target_restricted_use=target_restricted_use,
        )
        if dry_run:
            return {"ok": True, "dry_run": True, "external_id": external_id, "envelope": envelope}

        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                response = self.session.post(self.endpoint, data=envelope.encode("utf-8"), headers=headers, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SAPGoodsMovementError(f"SAP Goods Movement service unreachable: {e}")

        if response.status_code == 401:
            raise SAPGoodsMovementError("SAP SOAP authentication failed for Goods Movement.")
        if response.status_code != 200:
            raise SAPGoodsMovementError(f"SAP Goods Movement service responded with HTTP {response.status_code}: {response.text[:500]}")

        sap_error = _extract_sap_error(response.text)
        if sap_error:
            return {"ok": False, "external_id": external_id, "error": f"SAP rejected the movement: {sap_error}", "raw_xml": response.text}
        return {"ok": True, "external_id": _extract_gac_id(response.text) or external_id, "client_reference_id": external_id, "raw_xml": response.text}
```

### `job_store.py` (new file — minimal version, only what this flow needs)
MongoDB-backed background job tracking so the "Receive" button's status survives a page refresh
and works across multiple backend workers.

```python
from datetime import datetime, timezone

COLLECTION_NAME = "background_jobs"
JOB_RETENTION_SECONDS = 7 * 86400


def ensure_indexes(db) -> None:
    try:
        db[COLLECTION_NAME].create_index("created_at", expireAfterSeconds=JOB_RETENTION_SECONDS, name="created_at_1")
    except Exception:
        db.command("collMod", COLLECTION_NAME, index={"keyPattern": {"created_at": 1}, "expireAfterSeconds": JOB_RETENTION_SECONDS})


def create_job(db, job_id: str, initial_state: dict) -> None:
    db[COLLECTION_NAME].insert_one({"_id": job_id, "created_at": datetime.now(timezone.utc), **initial_state})


def update_job(db, job_id: str, update: dict) -> None:
    db[COLLECTION_NAME].update_one({"_id": job_id}, {"$set": update})


def get_job(db, job_id: str):
    doc = db[COLLECTION_NAME].find_one({"_id": job_id})
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc
```

### `store_approval_service.py` — extract just this part (or put it in `inbound_receipt_service.py` directly)
The retry wrapper + SAP-error-in-a-200-response guard around the Goods Movement call.

```python
import re

_GOODS_MOVEMENT_MAX_ATTEMPTS = 3
_GOODS_MOVEMENT_RETRY_DELAY_SECONDS = 5


def _has_sap_log_error(result: dict) -> str | None:
    """SAP can embed a real error inside a normal-looking <Log> block (SeverityCode 3=error)
    rather than raising a SOAP fault - must be checked even when the HTTP call itself succeeds."""
    xml = (result or {}).get("raw_xml") or (result or {}).get("envelope") or (result or {}).get("raw") or ""
    if not xml:
        return None
    if re.search(r"<SeverityCode>\s*[3-9]\s*</SeverityCode>", xml):
        note = re.search(r"<Note>(.*?)</Note>", xml)
        return note.group(1) if note else "SAP logged an error-severity item for this movement"
    return None


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

### `inbound_receipt_service.py` (new file, full — the core orchestration)

```python
"""Inbound STO Receipt - fully synchronous flow, zero polling/webhooks/background sweeps.

Order, one pass per delivery:
  Acknowledge -> PostGoodsReceipt directly (Path A, primary - NEVER call Release first, it
  disables PGRBackground for this tenant once already-released-without-GR).
  If PostGoodsReceipt is rejected: Release -> immediate Warehouse-Order lookup -> ConfirmAsPlanned
  (Path B fallback, still same synchronous pass). If that also finds nothing, raise immediately -
  the user retries manually a moment later. Never parks, never waits, never retries in a loop.

Then: Goods Movement relocation from {SITE}-HOLD to the STO's real destination warehouse."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import job_store
from sap_rate_limiter import SAP_MAX_CONCURRENT_REQUESTS
from sap_wip_clearing_client import company_and_set_of_books_for_site, inbound_staging_area_for_site
from store_approval_service import _trigger_goods_movement

logger = logging.getLogger(__name__)

STO_COLLECTION = "stock_transfer_orders"
_PENDING_QUERY = {"gi_status": "posted", "receipt_status": {"$nin": ["received"]}}
_SAP_FINISHED_STATUS_CODE = "3"


def _receipt_hold_warehouse_id(site_id: str) -> str:
    return inbound_staging_area_for_site(site_id)


def _relocate_receipt_from_hold(db, sap_goods_movement_client, doc: dict, quantity_overrides: dict = None) -> dict:
    """Called right after a real Goods Receipt is confirmed. Never raises - failure here must
    never undo the already-successful receipt; caller stores the result. Fires all lines
    concurrently (still capped at SAP_MAX_CONCURRENT_REQUESTS in-flight via sap_semaphore)."""
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
            result = future.result()
            if not result.get("ok"):
                raw = result.get("error_detail") or result.get("error") or ""
                if re.search(r"negative stock not permitted", raw, re.IGNORECASE):
                    error = "Stock does not exist in the STO warehouse"
                else:
                    error = result.get("error") or raw or "unknown SAP error"
                line_results.append({"product_id": item["product_id"], "ok": False, "error": error})
            else:
                line_results.append({"product_id": item["product_id"], "ok": True, "gac_id": result.get("external_id")})
    all_ok = all(r["ok"] for r in line_results)
    any_ok = any(r["ok"] for r in line_results)
    status = "done" if all_ok else ("partial" if any_ok else "failed")
    return {"status": status, "to": ship_to_location_id, "lines": line_results}


def retry_receipt_relocation(db, sap_goods_movement_client, sto_id: str) -> dict:
    doc = db[STO_COLLECTION].find_one({"_id": sto_id})
    if not doc:
        raise ValueError(f"Stock Transfer Order {sto_id} not found.")
    if doc.get("receipt_status") not in ("received", "partial", "failed"):
        raise ValueError("This order must be received before retrying the warehouse move.")
    result = _relocate_receipt_from_hold(db, sap_goods_movement_client, doc)
    status_map = {"done": "received", "partial": "partial", "failed": "failed"}
    overall = status_map.get(result.get("status"), doc.get("receipt_status"))
    lines = result.get("lines") or []
    error_summary = " | ".join(f"{l['product_id']}: {l['error']}" for l in lines if not l["ok"]) or None
    db[STO_COLLECTION].update_one({"_id": sto_id}, {"$set": {
        "receipt_relocation": result, "receipt_status": overall, "receipt_error": error_summary,
        "receipt_results": lines or doc.get("receipt_results"),
    }})
    return result


def list_ship_to_sites_with_pending_receipts(db) -> list:
    return sorted(s for s in db[STO_COLLECTION].distinct("ship_to_site_id", _PENDING_QUERY) if s)


def list_pending_receipts(db, site_id: str = None) -> list:
    """NOTE: the original also backfills/verifies outbound_delivery_ids/inbound_delivery_ids via
    extra SAP clients (sap_outbound_delivery_client, sap_inbound_delivery_client) - that logic is
    tied to how YOUR app tracks the STO's own SAP delivery references from Goods Issue. Simplify
    this to just read whatever your STO doc already stores in `outbound_delivery_ids`, or port
    `_backfill_missing_delivery_ids`/`_verify_inbound_delivery_notification` from the original file
    if you need the same live-verification safety net."""
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
            "receipt_error": doc.get("receipt_error"),
            "outbound_delivery_ids": doc.get("outbound_delivery_ids") or [],
            "items": [
                {"line_no": it.get("line_no"), "product_id": it.get("product_id"), "description": it.get("description"),
                 "unit_of_measure": it.get("unit_of_measure"), "requested_qty": it.get("requested_qty")}
                for it in (doc.get("items") or [])
            ],
        })
    active_jobs = {
        j["sto_id"]: {"job_id": j["_id"], "phase": j.get("phase"), "progress_current": j.get("progress_current"), "progress_total": j.get("progress_total")}
        for j in db[job_store.COLLECTION_NAME].find({"sto_id": {"$in": [r["sto_id"] for r in results]}, "kind": "inbound_receipt", "status": "running"})
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
        "receipt_error": doc.get("receipt_error"),
        "received_at": doc.get("received_at"),
        "receipt_completed_at": doc.get("receipt_completed_at"),
        "receipt_duration_seconds": doc.get("receipt_duration_seconds"),
        "items_count": len(doc.get("items") or []),
        "receipt_relocation": doc.get("receipt_relocation"),
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
    """Called once every delivery has a real SAP Goods Receipt posted - moves stock from
    {SITE}-HOLD to the STO's real destination."""
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
    """quantity_overrides: {str(line_no): received_qty}. Returns {product_id: qty} for only
    lines that differ from the full shipped amount."""
    quantity_overrides = quantity_overrides or {}
    result = {}
    for it in (doc.get("items") or []):
        override = quantity_overrides.get(str(it["line_no"]))
        if override is not None and abs(float(override) - float(it["requested_qty"])) > 1e-6:
            result[it["product_id"]] = float(override)
    return result


def _find_matching_lot(sap_execution_client, site_id: str, product_ids: list) -> dict:
    """Path B fallback only. Single immediate lookup, no retry, no wait."""
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
    sap_goods_movement_client, sto_id: str, actor: str, quantity_overrides: dict = None,
) -> dict:
    """THE MAIN ENTRY POINT - this is what the "Receive" button calls."""
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
            # PATH A - direct PostGoodsReceipt, NO Release call. Calling Release first is what
            # disables PGRBackground for this tenant once a delivery is already-released-without-GR.
            sap_inbound_delivery_client.post_goods_receipt(found["object_id"])
        except Exception as e:
            pgr_error = e
        if pgr_error is None:
            continue  # Path A succeeded, real GR posted
        # PATH B fallback - this delivery rejected direct PGR (already Released-but-unfinished).
        try:
            sap_kh_inbound_delivery_client.release_delivery(found["object_id"])
        except Exception as e:
            logger.info(f"Release for {delivery_id} ({sto_id}) skipped/failed (may already be released): {e}")
        refreshed = sap_kh_inbound_delivery_client.find_object_id(delivery_id) or found
        if refreshed.get("delivery_processing_status_code") == _SAP_FINISHED_STATUS_CODE:
            continue  # Release itself finished it
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
    return receive_stock_transfer_order(db, sap_goods_movement_client, sto_id, actor, quantity_overrides)
```

## 3. `server.py` wiring — client instantiation + routes

```python
import inbound_receipt_service
from sap_inbound_delivery_client import SAPInboundDeliveryClient
from sap_inbound_delivery_execution_client import SAPInboundDeliveryExecutionClient
from sap_goods_movement_client import SAPGoodsMovementClient

sap_inbound_delivery_client = SAPInboundDeliveryClient(
    endpoint=os.environ['SAP_ODATA_INBOUND_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
    vhost=os.environ['BYD_ODATA_VHOST'],
)

# Same underlying SAP object as above, just pointed at `khinbounddelivery` - the only service
# exposing AcknowledgeDeliveryNoteReceipt (a required prerequisite before Release).
sap_kh_inbound_delivery_client = SAPInboundDeliveryClient(
    endpoint=os.environ['SAP_ODATA_KH_INBOUND_DELIVERY_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
    vhost=os.environ['BYD_ODATA_VHOST'],
    release_action="Release",
)

sap_inbound_delivery_execution_client = SAPInboundDeliveryExecutionClient(
    endpoint=os.environ['SAP_ODATA_INBOUND_DELIVERY_EXECUTION_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
    vhost=os.environ['BYD_ODATA_VHOST'],
)

sap_goods_movement_client = SAPGoodsMovementClient(
    endpoint=os.environ['SAP_SOAP_GOODS_MOVEMENT_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)


# ==================== Inbound STO Receipt routes ====================

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

    for it in payload.items:
        sto_line = next((x for x in doc.get("items") or [] if x["line_no"] == it.line_no), None)
        if sto_line and (it.received_qty <= 0 or it.received_qty > sto_line["requested_qty"] + 1e-6):
            raise HTTPException(status_code=400, detail=f"Received Qty for line {it.line_no} must be between 0 and the shipped quantity ({sto_line['requested_qty']}).")

    job_id = str(uuid.uuid4())
    receipt_started_at = datetime.now(timezone.utc)
    await asyncio.to_thread(db[inbound_receipt_service.STO_COLLECTION].update_one, {"_id": sto_id}, {"$set": {"receipt_started_at": receipt_started_at}})
    await asyncio.to_thread(job_store.create_job, db, job_id, {
        "sto_id": sto_id, "kind": "inbound_receipt", "status": "running", "phase": "queued",
        "progress_current": 0, "progress_total": 1, "result": None, "error": None,
    })

    async def run():
        try:
            await asyncio.to_thread(job_store.update_job, db, job_id, {"phase": "processing"})
            final = await asyncio.to_thread(
                inbound_receipt_service.start_automated_receipt, db,
                sap_kh_inbound_delivery_client, sap_inbound_delivery_client, sap_inbound_delivery_execution_client,
                sap_goods_movement_client, sto_id, actor, overrides,
            )
            await asyncio.to_thread(job_store.update_job, db, job_id, {"status": "done", "phase": "done", "result": final, "error": None})
        except Exception as e:
            logger.error(f"Inbound receipt job {job_id} ({sto_id}) failed: {e}")
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


@api_router.post("/inbound-receipts/{sto_id}/retry-receipt-relocation")
async def post_inbound_receipt_retry_relocation(sto_id: str):
    try:
        result = await asyncio.to_thread(inbound_receipt_service.retry_receipt_relocation, db, sap_goods_movement_client, sto_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
```

## 4. MongoDB doc shape expected on `stock_transfer_orders`

The receive flow reads/writes these fields on each STO doc (`_id` = your own STO ID string):

```json
{
  "_id": "STO-000142",
  "sap_order_id": "0000032871",
  "ship_from_site_id": "P9",
  "ship_to_site_id": "P2",
  "ship_to_location_id": "P2-RM",
  "ship_to_location_name": "P2 Raw Material",
  "gi_status": "posted",
  "outbound_delivery_ids": ["P9D1-431"],
  "items": [
    {"line_no": 1, "product_id": "G12NUT", "description": "...", "unit_of_measure": "EA", "requested_qty": 100}
  ],
  "receipt_status": "pending",
  "created_at": "2026-09-22T10:00:00Z"
}
```
`gi_status: "posted"` + a non-empty `outbound_delivery_ids` are the 2 hard gates before Receive
is even allowed (your own STO-creation/Goods-Issue flow needs to set these — not part of this
bundle).

## 5. Frontend — `InboundReceiptsPage.js` (full file, copy verbatim)

Uses `axios`, `@phosphor-icons/react`, and shadcn/ui components (`button`, `input`, `checkbox`,
`sonner`, `select`, `dialog`, `table`, `popover`) — same set as the original app. Adjust the
`NavTabs`/`SapConnectionStatus`/`ErpConnectionStatus` imports (lines 14-16) to your own app's
header components, or remove them.

```jsx
import { useState, useEffect, useCallback, useRef, Fragment } from "react";
import axios from "axios";
import { Truck, Shield, CircleNotch, CaretDown, CaretUp, CheckCircle, ArrowRight, WarningCircle } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Toaster, toast } from "@/components/ui/sonner";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const formatQty = (v) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 }));
const formatDate = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const formatDuration = (seconds) => {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
};

const COMPLETED_STATUS_STYLE = {
  received: { label: "Received", cls: "bg-[#ECFDF3] text-[#027A48]" },
  partial: { label: "Partially Received", cls: "bg-[#FEF3C7] text-[#92400E]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

const STATUS_STYLE = {
  pending: { label: "Pending Receipt", cls: "bg-[#FEF3C7] text-[#92400E]" },
  partial: { label: "Partially Received", cls: "bg-[#FEE4E2] text-[#B42318]" },
  failed: { label: "Receipt Failed", cls: "bg-[#FEE4E2] text-[#B42318]" },
};

const jobBadge = (job) => {
  if (!job) return null;
  const receiptFailed = job.status === "failed" || (job.status === "done" && job.result && job.result.status !== "received");
  if (receiptFailed) {
    return { icon: WarningCircle, cls: "text-[#B42318]", iconCls: "", label: "Failed", detail: job.error || job.result?.error };
  }
  if (job.status === "done") {
    return { icon: CheckCircle, cls: "text-[#027A48]", iconCls: "", label: "Done", detail: null };
  }
  return { icon: CircleNotch, cls: "text-[#0B6B74]", iconCls: "animate-spin", label: "Receiving…", detail: null };
};

const RelocationPopover = ({ relocation, stoId, children }) => (
  <Popover>
    <PopoverTrigger asChild>
      <button className="underline-offset-2 hover:underline text-left" data-testid={`inbound-receipts-completed-relocation-trigger-${stoId}`}>
        {children}
      </button>
    </PopoverTrigger>
    <PopoverContent className="w-72 p-3" align="start" data-testid={`inbound-receipts-completed-relocation-popover-${stoId}`}>
      <p className="text-xs font-semibold text-[#101828] mb-2">Warehouse Move — {relocation.to}</p>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[#667085]">
            <th className="text-left font-medium py-1">Item</th>
            <th className="text-right font-medium py-1">Movement</th>
          </tr>
        </thead>
        <tbody>
          {(relocation.lines || []).map((l) => (
            <tr key={l.product_id} className="border-t border-[#EAECF0]">
              <td className="py-1.5 pr-2 font-mono text-[#344054]">{l.product_id}</td>
              <td className="py-1.5 text-right font-mono">
                {l.ok ? <span className="text-[#027A48]">GM {l.gac_id}</span> : <span className="text-[#B42318]">{l.error}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </PopoverContent>
  </Popover>
);

export default function InboundReceiptsPage() {
  const [sites, setSites] = useState([]);
  const [siteFilter, setSiteFilter] = useState("all");
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  const [activeJobs, setActiveJobs] = useState({});
  const activeJobsRef = useRef(activeJobs);
  activeJobsRef.current = activeJobs;
  const [activeTab, setActiveTab] = useState("pending");
  const [completedOrders, setCompletedOrders] = useState([]);
  const [completedLoading, setCompletedLoading] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [retryingRelocationStoId, setRetryingRelocationStoId] = useState(null);

  const handleRetryReceiptRelocation = async (stoId) => {
    setRetryingRelocationStoId(stoId);
    try {
      await axios.post(`${API}/inbound-receipts/${stoId}/retry-receipt-relocation`);
      toast.success("Warehouse move retried.");
      loadCompletedOrders();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not retry the warehouse move.");
    } finally {
      setRetryingRelocationStoId(null);
    }
  };

  useEffect(() => {
    axios.get(`${API}/inbound-receipts/sites`).then(({ data }) => {
      setSites(data.sites || []);
    }).catch(() => toast.error("Could not load receiving sites."));
  }, []);

  const loadCompletedOrders = useCallback(async () => {
    setCompletedLoading(true);
    try {
      const params = {};
      if (siteFilter !== "all") params.site_id = siteFilter;
      if (dateFrom) params.date_from = dateFrom;
      if (dateTo) params.date_to = dateTo;
      const { data } = await axios.get(`${API}/inbound-receipts/completed`, { params });
      setCompletedOrders(data.orders || []);
    } catch {
      toast.error("Could not load completed receipts.");
    } finally {
      setCompletedLoading(false);
    }
  }, [siteFilter, dateFrom, dateTo]);

  useEffect(() => {
    if (activeTab === "completed") loadCompletedOrders();
  }, [activeTab, loadCompletedOrders]);

  const loadOrders = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await axios.get(`${API}/inbound-receipts/pending`, {
        params: siteFilter === "all" ? {} : { site_id: siteFilter },
      });
      setOrders(data.orders || []);
      setActiveJobs((prev) => {
        const next = { ...prev };
        (data.orders || []).forEach((o) => {
          if (o.active_job?.job_id && !next[o.sto_id]) {
            next[o.sto_id] = { job_id: o.active_job.job_id, status: "running", phase: o.active_job.phase, progress_current: o.active_job.progress_current, progress_total: o.active_job.progress_total };
          }
        });
        return next;
      });
    } catch {
      toast.error("Could not load pending receipts.");
    } finally {
      setLoading(false);
    }
  }, [siteFilter]);

  useEffect(() => { loadOrders(); }, [loadOrders]);

  useEffect(() => {
    const hasLive = Object.values(activeJobs).some((j) => j.status === "running");
    if (!hasLive) return;
    const interval = setInterval(async () => {
      const current = activeJobsRef.current;
      const liveEntries = Object.entries(current).filter(([, j]) => j.status === "running");
      if (liveEntries.length === 0) return;
      const updates = await Promise.all(liveEntries.map(async ([stoId, job]) => {
        try {
          const { data } = await axios.get(`${API}/inbound-receipts/receive-status/${job.job_id}`);
          return [stoId, { ...job, status: data.status, phase: data.phase, error: data.error, result: data.result }];
        } catch {
          return [stoId, job];
        }
      }));
      let anyFinished = false;
      setActiveJobs((prev) => {
        const next = { ...prev };
        updates.forEach(([stoId, job]) => {
          next[stoId] = job;
          if (job.status === "done" || job.status === "failed") anyFinished = true;
        });
        return next;
      });
      if (anyFinished) {
        loadOrders();
        setTimeout(() => {
          setActiveJobs((prev) => {
            const next = {};
            Object.entries(prev).forEach(([stoId, job]) => {
              if (job.status === "running") next[stoId] = job;
            });
            return next;
          });
        }, 6000);
      }
    }, 2500);
    return () => clearInterval(interval);
  }, [activeJobs, loadOrders]);

  const startReceiveJobs = async (targetOrders) => {
    const results = await Promise.all(targetOrders.map(async (order) => {
      const items = order.items.map((it) => ({ line_no: it.line_no, received_qty: it.requested_qty }));
      try {
        const { data } = await axios.post(`${API}/inbound-receipts/${order.sto_id}/receive`, { items });
        return { stoId: order.sto_id, jobId: data.job_id, alreadyReceived: data.already_received };
      } catch (e) {
        return { stoId: order.sto_id, error: e?.response?.data?.detail || "Could not start" };
      }
    }));
    setActiveJobs((prev) => {
      const next = { ...prev };
      results.forEach((r) => {
        if (r.alreadyReceived) return;
        next[r.stoId] = r.jobId
          ? { job_id: r.jobId, status: "running", phase: "processing" }
          : { status: "failed", error: r.error };
      });
      return next;
    });
    return results;
  };

  const handleReceiveOne = async (order) => {
    const [result] = await startReceiveJobs([order]);
    if (result.alreadyReceived) {
      toast.success(`${order.sto_id} was already received.`);
      loadOrders();
    } else if (!result.jobId) {
      toast.error(result.error || "Could not start the receipt.");
    }
  };

  const toggleSelected = (stoId, checked) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(stoId); else next.delete(stoId);
      return next;
    });
  };

  const selectableOrders = orders.filter((o) => !activeJobs[o.sto_id] || activeJobs[o.sto_id].status !== "running");
  const allSelected = selectableOrders.length > 0 && selectableOrders.every((o) => selectedIds.has(o.sto_id));
  const toggleSelectAll = (checked) => {
    setSelectedIds(checked ? new Set(selectableOrders.map((o) => o.sto_id)) : new Set());
  };

  const submitBulkReceive = async () => {
    const targets = orders.filter((o) => selectedIds.has(o.sto_id));
    setBulkSubmitting(true);
    try {
      const results = await startReceiveJobs(targets);
      const started = results.filter((r) => r.jobId).length;
      toast.success(`Started receiving ${started} order${started === 1 ? "" : "s"}.`);
      setSelectedIds(new Set());
    } finally {
      setBulkSubmitting(false);
      setBulkConfirmOpen(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F9FAFB]" data-testid="inbound-receipts-page">
      <Toaster position="top-right" richColors />
      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 py-8">
        <div className="flex items-start justify-between gap-4 flex-wrap mb-6">
          <div>
            <h1 className="font-heading text-2xl sm:text-3xl font-bold text-[#101828] flex items-center gap-2">
              <Truck size={28} weight="fill" className="text-[#0B6B74]" />
              Inbound STO Receipt
            </h1>
            <p className="text-sm text-[#667085] mt-1">
              Receive a Stock Transfer Order in one click — closes every SAP delivery line for it automatically.
            </p>
          </div>
          <div className="w-full sm:w-56">
            <Select value={siteFilter} onValueChange={setSiteFilter}>
              <SelectTrigger data-testid="inbound-receipts-site-filter">
                <SelectValue placeholder="Receiving site" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Receiving Sites</SelectItem>
                {sites.map((s) => (
                  <SelectItem key={s} value={s}>{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex items-center gap-1 mb-5 bg-[#EEF2F1] p-1 rounded-lg w-fit" data-testid="inbound-receipts-tabs">
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${activeTab === "pending" ? "bg-white text-[#0B6B74] shadow-sm" : "text-[#667085] hover:text-[#344054]"}`}
            onClick={() => setActiveTab("pending")}
            data-testid="inbound-receipts-tab-pending"
          >
            Pending
          </button>
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${activeTab === "completed" ? "bg-white text-[#0B6B74] shadow-sm" : "text-[#667085] hover:text-[#344054]"}`}
            onClick={() => setActiveTab("completed")}
            data-testid="inbound-receipts-tab-completed"
          >
            Completed
          </button>
        </div>

        {activeTab === "completed" && (
          <div className="flex items-end gap-3 flex-wrap mb-5" data-testid="inbound-receipts-completed-filters">
            <div>
              <label className="text-xs font-medium text-[#667085] block mb-1">From</label>
              <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-40" data-testid="inbound-receipts-date-from" />
            </div>
            <div>
              <label className="text-xs font-medium text-[#667085] block mb-1">To</label>
              <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-40" data-testid="inbound-receipts-date-to" />
            </div>
            {(dateFrom || dateTo) && (
              <Button variant="outline" size="sm" onClick={() => { setDateFrom(""); setDateTo(""); }} data-testid="inbound-receipts-date-clear-btn">
                Clear dates
              </Button>
            )}
          </div>
        )}

        {activeTab === "pending" && (
        <Fragment>
        {selectedIds.size > 0 && (
          <div className="flex items-center justify-between bg-[#F0F9FA] border border-[#B4E4E8] rounded-lg px-4 py-2.5 mb-4" data-testid="inbound-receipts-bulk-bar">
            <span className="text-sm font-medium text-[#0B6B74]">{selectedIds.size} order{selectedIds.size === 1 ? "" : "s"} selected</span>
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => setSelectedIds(new Set())} data-testid="inbound-receipts-bulk-clear-btn">Clear</Button>
              <Button size="sm" onClick={() => setBulkConfirmOpen(true)} data-testid="inbound-receipts-bulk-receive-btn">
                Receive Selected ({selectedIds.size})
              </Button>
            </div>
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-24 text-[#667085]" data-testid="inbound-receipts-loading">
            <CircleNotch size={24} className="animate-spin mr-2" /> Loading pending receipts…
          </div>
        ) : orders.length === 0 ? (
          <div className="text-center py-24 text-[#667085] bg-white rounded-xl border border-[#EAECF0]" data-testid="inbound-receipts-empty">
            Nothing pending — every shipped STO for this site has been received.
          </div>
        ) : (
          <div className="bg-white rounded-xl border border-[#EAECF0] overflow-x-auto" data-testid="inbound-receipts-list">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <Checkbox
                      checked={allSelected}
                      onCheckedChange={(v) => toggleSelectAll(!!v)}
                      data-testid="inbound-receipt-select-all"
                    />
                  </TableHead>
                  <TableHead>STO</TableHead>
                  <TableHead>Route</TableHead>
                  <TableHead>Ship To</TableHead>
                  <TableHead>Shipped On</TableHead>
                  <TableHead>Lines</TableHead>
                  <TableHead className="min-w-[220px]">Status</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {orders.map((order) => {
                  const expanded = expandedId === order.sto_id;
                  const status = STATUS_STYLE[order.receipt_status] || STATUS_STYLE.pending;
                  const job = activeJobs[order.sto_id];
                  const badge = jobBadge(job);
                  const rowBusy = job && job.status === "running";
                  return (
                    <Fragment key={order.sto_id}>
                      <TableRow data-testid={`inbound-receipt-row-${order.sto_id}`}>
                        <TableCell>
                          <Checkbox
                            checked={selectedIds.has(order.sto_id)}
                            onCheckedChange={(v) => toggleSelected(order.sto_id, !!v)}
                            disabled={rowBusy}
                            data-testid={`inbound-receipt-select-${order.sto_id}`}
                          />
                        </TableCell>
                        <TableCell>
                          <button
                            className="flex items-center gap-1 font-semibold text-[#101828] hover:text-[#0B6B74]"
                            onClick={() => setExpandedId(expanded ? null : order.sto_id)}
                            data-testid={`inbound-receipt-toggle-${order.sto_id}`}
                          >
                            {expanded ? <CaretUp size={14} /> : <CaretDown size={14} />}
                            {order.sto_id}
                          </button>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-sm text-[#344054]">
                            {order.ship_from_site_id} <ArrowRight size={12} /> {order.ship_to_site_id}
                          </span>
                        </TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.ship_to_location_name || "—"}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{formatDate(order.created_at)}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.items.length}</TableCell>
                        <TableCell>
                          <span className={`text-xs font-medium px-2 py-1 rounded-full ${status.cls}`} data-testid={`inbound-receipt-status-${order.sto_id}`}>
                            {status.label}
                          </span>
                          {order.receipt_error && (
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs truncate" title={order.receipt_error}>{order.receipt_error}</div>
                          )}
                          {badge && (
                            <div className={`flex items-center gap-1 text-xs font-medium mt-1 whitespace-nowrap ${badge.cls}`} title={badge.detail || ""} data-testid={`inbound-receipt-job-badge-${order.sto_id}`}>
                              <badge.icon size={13} className={`shrink-0 ${badge.iconCls}`} />
                              <span className="truncate">{badge.label}</span>
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button size="sm" onClick={() => handleReceiveOne(order)} disabled={rowBusy} data-testid={`inbound-receipt-receive-btn-${order.sto_id}`}>
                            {rowBusy ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}
                            Receive
                          </Button>
                        </TableCell>
                      </TableRow>
                      {expanded && (
                        <TableRow key={`${order.sto_id}-detail`}>
                          <TableCell colSpan={8} className="bg-[#F9FAFB]">
                            <div className="py-2">
                              <table className="w-full text-sm">
                                <thead>
                                  <tr className="text-[#667085] text-xs">
                                    <th className="text-left py-1">Line</th>
                                    <th className="text-left py-1">Product</th>
                                    <th className="text-left py-1">Description</th>
                                    <th className="text-right py-1">Qty</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {order.items.map((it) => (
                                    <tr key={it.line_no} data-testid={`inbound-receipt-item-${order.sto_id}-${it.line_no}`}>
                                      <td className="py-1">{it.line_no}</td>
                                      <td className="py-1 font-mono text-xs">{it.product_id}</td>
                                      <td className="py-1">{it.description}</td>
                                      <td className="py-1 text-right">{formatQty(it.requested_qty)} {it.unit_of_measure}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
        </Fragment>
        )}

        {activeTab === "completed" && (
          completedLoading ? (
            <div className="flex items-center justify-center py-24 text-[#667085]" data-testid="inbound-receipts-completed-loading">
              <CircleNotch size={24} className="animate-spin mr-2" /> Loading completed receipts…
            </div>
          ) : completedOrders.length === 0 ? (
            <div className="text-center py-24 text-[#667085] bg-white rounded-xl border border-[#EAECF0]" data-testid="inbound-receipts-completed-empty">
              No completed receipts found for this filter.
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-[#EAECF0] overflow-x-auto" data-testid="inbound-receipts-completed-list">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>STO</TableHead>
                    <TableHead>Route</TableHead>
                    <TableHead>Ship To</TableHead>
                    <TableHead>Received At</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Warehouse Move</TableHead>
                    <TableHead className="text-right">Time Taken</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {completedOrders.map((order) => {
                    const status = COMPLETED_STATUS_STYLE[order.receipt_status] || COMPLETED_STATUS_STYLE.received;
                    const relocation = order.receipt_relocation;
                    return (
                      <TableRow key={order.sto_id} data-testid={`inbound-receipts-completed-row-${order.sto_id}`}>
                        <TableCell>
                          <span className="font-semibold text-[#101828]">{order.sto_id}</span>
                          <div className="text-xs text-[#667085]">SAP #{order.sap_order_id}</div>
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex items-center gap-1 text-sm text-[#344054]">
                            {order.ship_from_site_id} <ArrowRight size={12} /> {order.ship_to_site_id}
                          </span>
                        </TableCell>
                        <TableCell className="text-sm text-[#344054]">{order.ship_to_location_name || "—"}</TableCell>
                        <TableCell className="text-sm text-[#344054]">{formatDate(order.received_at || order.receipt_completed_at)}</TableCell>
                        <TableCell>
                          <span className={`text-xs font-medium px-2 py-1 rounded-full ${status.cls}`} data-testid={`inbound-receipts-completed-status-${order.sto_id}`}>
                            {status.label}
                          </span>
                          {order.receipt_error && (
                            <div className="text-xs text-[#B42318] mt-1 max-w-xs truncate" title={order.receipt_error}>{order.receipt_error}</div>
                          )}
                        </TableCell>
                        <TableCell data-testid={`inbound-receipts-completed-relocation-${order.sto_id}`}>
                          {!relocation ? (
                            <span className="text-xs text-[#98A2B3]">—</span>
                          ) : relocation.status === "skipped_same_warehouse" ? (
                            <span className="text-xs text-[#667085]">Received directly into {relocation.to || order.ship_to_location_name}</span>
                          ) : relocation.status === "done" ? (
                            <RelocationPopover relocation={relocation} stoId={order.sto_id}>
                              <span className="text-xs text-[#027A48]">Moved to {relocation.to}</span>
                            </RelocationPopover>
                          ) : (
                            <div className="flex items-center gap-2">
                              <RelocationPopover relocation={relocation} stoId={order.sto_id}>
                                <span className="text-xs text-[#B42318]">
                                  {relocation.status === "partial" ? "Partially moved" : "Move failed"}
                                </span>
                              </RelocationPopover>
                              <Button
                                size="sm" variant="outline"
                                onClick={() => handleRetryReceiptRelocation(order.sto_id)}
                                disabled={retryingRelocationStoId === order.sto_id}
                                data-testid={`inbound-receipts-completed-retry-relocation-${order.sto_id}`}
                              >
                                {retryingRelocationStoId === order.sto_id ? <CircleNotch size={14} className="animate-spin mr-1" /> : null}Retry
                              </Button>
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm text-[#344054]" data-testid={`inbound-receipts-completed-duration-${order.sto_id}`}>
                          {formatDuration(order.receipt_duration_seconds)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )
        )}
      </div>

      <Dialog open={bulkConfirmOpen} onOpenChange={(open) => !bulkSubmitting && setBulkConfirmOpen(open)}>
        <DialogContent className="max-w-md" data-testid="inbound-receipt-bulk-dialog">
          <DialogHeader>
            <DialogTitle>Receive {selectedIds.size} order{selectedIds.size === 1 ? "" : "s"}?</DialogTitle>
            <DialogDescription>
              Each order will be received at its full shipped quantity and closed in SAP. This runs in the
              background — you can keep working, progress shows on each row.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBulkConfirmOpen(false)} disabled={bulkSubmitting} data-testid="inbound-receipt-bulk-cancel-btn">
              Cancel
            </Button>
            <Button onClick={submitBulkReceive} disabled={bulkSubmitting} data-testid="inbound-receipt-bulk-confirm-btn">
              {bulkSubmitting ? <><CircleNotch size={16} className="animate-spin mr-2" /> Starting…</> : "Confirm"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
```

Add the route in your new app's router (e.g. `App.js`):
```jsx
<Route path="/inbound-receipts" element={<InboundReceiptsPage />} />
```

## 6. Checklist to wire this into the new app
1. Copy the 6 backend files (section 2) into the new app's `backend/` folder.
2. Add the `.env` keys (section 1) — real URLs above, your own credential values.
3. Add `job_store.ensure_indexes(db)` to your startup block (once).
4. Wire the client instantiations + 5 routes into your `server.py` (section 3) — swap the
   `auth_service.get_current_user` line for your own auth.
5. Make sure your STO-creation flow populates `gi_status`, `outbound_delivery_ids`,
   `ship_to_site_id`, `ship_to_location_id`, `items[].product_id/unit_of_measure/requested_qty`
   on `stock_transfer_orders` (section 4) — this bundle assumes that already exists.
6. Copy `InboundReceiptsPage.js` (section 5) into `frontend/src/pages/`, add the route.
7. Set `SAP_GOODS_MOVEMENT_DRY_RUN="false"` only once you've reviewed a batch of dry runs — it
   defaults to `"true"` (safe, no real SAP write) if unset.
8. Test with one real never-touched STO end-to-end before trusting it for daily use.
