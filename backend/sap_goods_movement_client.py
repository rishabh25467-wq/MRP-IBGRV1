"""Direct SAP Business ByDesign Goods Movement client (Aug 2026).

Replaces the Radish QMS REST wrapper (radish_qms_client.py) for the Store
Approval "issue stock" action ONLY - per user's explicit choice, everywhere
else in this app (sales forecast, etc.) keeps using Radish. Calls SAP's
own `InventoryProcessingGoodsAndActivityConfirmationGoodsMovementIn.
DoGoodsMovement` SOAP service directly, using the SAME technical user
(`_EMERGENTBOM`) and endpoint Radish's own backend uses for this exact
service (confirmed live via Radish's own `GET /api/byd/writeback/config`
health-check endpoint - `auth_user: "_EMERGENTBOM"`, same credential this
app already holds in SAP_SOAP_USERNAME/PASSWORD for every other SOAP call).

Envelope shape mirrors Radish's own proven dry-run output exactly (live-
verified against this tenant several times this session) - this app's use
case never flips the restricted-use flag (always plain RM -> SFG, same
flag both sides), so the "2-step move" complexity Radish's wrapper handles
for flag-flipping moves does not apply here.

Aug 2026: also reused by the Supplier Portal's GRN approval (Phase 4,
step 2) to land a received PO's stock into the receiver's chosen
warehouse with an explicit RESTRICTED Quality Inspection status -
`target_stock_status_code="1"` + `target_restricted_use=True` (SAP's
InventoryStockStatusCode="1" = in inspection, InventoryRestrictedUseIndicator
governs the RESTRICTED flag) - both default to the original plain-move
behavior (blank/false) for every other existing caller."""
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


# SAP's own <InventoryItemChangeQuantity> expects the `unitCode` attribute
# to be a real UN/CEFACT Recommendation 20 code (max 3 chars, e.g. "KGM"
# for kilogram) - completely separate from `QuantityTypeCode` (a
# dimension label like "MASS"/"EA"). This app's own `unit_of_measure`
# field (sourced from SAP's own BOM <InputProductQuantityUoM>) is
# actually that QuantityTypeCode dimension, not a real unit code - a real
# live call with unitCode="MASS" (4 chars) was rejected by SAP with
# "Value is longer than the maximum permitted length 3" (confirmed via
# SAP's own Application Log). Map the dimension labels we've actually
# seen in this app's data to their real UN/CEFACT unit code; anything
# already <=3 chars (e.g. "EA") passes through unchanged since it can't
# hit this specific failure.
_UNIT_CODE_MAP = {"MASS": "KGM"}


def _resolve_unit_code(quantity_type_code: str) -> str:
    mapped = _UNIT_CODE_MAP.get(quantity_type_code)
    if mapped:
        return mapped
    if len(quantity_type_code) > 3:
        logger.warning(f"No _UNIT_CODE_MAP entry for '{quantity_type_code}' - truncating to 3 chars, SAP will likely reject an invalid code. Add a real UN/CEFACT mapping.")
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
    """Same shape SAP's own <Log> block uses everywhere else in this app
    (see store_approval_service._has_sap_log_error) - SeverityCode 3+ is a
    real rejection even on an HTTP 200 response."""
    m = re.search(r"<SeverityCode>\s*[3-9]\s*</SeverityCode>", xml)
    if not m:
        return None
    note = re.search(r"<Note>(.*?)</Note>", xml)
    return note.group(1) if note else "SAP logged an error-severity item for this movement"


def _extract_gac_id(xml: str):
    """Sep 9 2026 bug fix: SAP's response echoes back OUR OWN generated
    <ExternalGACID> (what this client sent as the request's business ID)
    alongside SAP's own real, permanent document number in <GACID> (e.g.
    "274343") - confirmed live via a real posted movement's raw response:
    <GACDetails><ExternalGACID>MOV-2B1632</ExternalGACID><GACUUID>...
    </GACUUID><GACID>274343</GACID></GACDetails>. Every caller/UI in this
    app was displaying ExternalGACID (our own throwaway placeholder,
    meaningless to anyone checking SAP directly) instead of this real
    GACID - fixed by returning GACID as `external_id` whenever SAP's
    response actually contains one."""
    m = re.search(r"<GACID>(.*?)</GACID>", xml)
    return m.group(1).strip() if m and m.group(1).strip() else None


# SAP's real Logistics Area ID for the write API is just "{SITE}-{TYPE}"
# (e.g. "P2-RM", "P8-SFG" - confirmed live via SAP's own Logistics Area ID
# lookup screen). This app's OWN data (inventory analytics report's
# CLOG_AREA_UUID, and the fixed RM->SFG business rule built from it)
# stores/constructs these as "{site}/{site}-TYPE" (e.g. "P2/P2-RM") - that
# extra "{site}/" is the analytics report's own composite display-key
# prefix, not part of the real ID, and SAP's write API rejected it as
# "Source logistics area is invalid" until stripped.
def _normalize_logistics_area_id(value: str) -> str:
    return value.rsplit("/", 1)[-1]


class SAPGoodsMovementClient:
    def __init__(self, endpoint: str, username: str, password: str, timeout: tuple = (8, 30)):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout

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
                response = requests.post(self.endpoint, data=envelope.encode("utf-8"), headers=headers, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SAPGoodsMovementError(f"SAP Goods Movement service unreachable: {e}")

        if response.status_code == 401:
            raise SAPGoodsMovementError("SAP SOAP authentication failed for Goods Movement (check SAP_SOAP_USERNAME/PASSWORD).")
        if response.status_code != 200:
            raise SAPGoodsMovementError(f"SAP Goods Movement service responded with HTTP {response.status_code}: {response.text[:500]}")

        sap_error = _extract_sap_error(response.text)
        if sap_error:
            return {"ok": False, "external_id": external_id, "error": f"SAP rejected the movement: {sap_error}", "raw_xml": response.text}
        return {"ok": True, "external_id": _extract_gac_id(response.text) or external_id, "client_reference_id": external_id, "raw_xml": response.text}
