"""SAP Business ByDesign QueryMaterialIn SOAP client - resolves a Material
ID (business ID/InternalID, e.g. 'SI-0038C-2') directly to its internal
system UUID via the MaterialByElementsQuery_sync operation. Unlike the BOM
SOAP client (sap_soap_client.py), this requires NO BOM relationship at all
- it's the only way to get a product_uuid (needed for the Standard Costs
join on the Inventory page) for items that are neither a BOM root nor ever
appear as anyone's ingredient, e.g. purchased raw materials with no BOM
anywhere in the explored catalog.

Authorized and working as of 08 Aug 2026 (the "materialquery" Communication
Scenario / QueryMaterialIn service was activated for the _EMERGENTBOM
business user)."""
import re
import uuid

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore


class SAPMaterialError(Exception):
    pass


class SAPMaterialAuthError(SAPMaterialError):
    """Raised specifically when SAP rejects the call due to a missing
    authorization role - distinct from a transient network/SOAP error, so
    callers can stop immediately instead of burning through retries/many
    items on every call in a batch that will all fail the same way."""
    pass


class SAPMaterialWriteNotConfiguredError(SAPMaterialError):
    """Raised when the write-back service (ManageMaterialIn) has no
    Communication Arrangement/Scenario set up on the tenant at all yet -
    live-confirmed (18 Aug 2026) this shows up as a generic 'Web service
    processing error' fault, NOT the specific 'Authorization role missing'
    fault QueryMaterialIn showed before ITS own arrangement existed (same
    distinction already used in sap_supplier_client.py)."""
    pass


# Quantity Characteristic type codes shown on the Material master's "UoM
# Characteristics" tab (per SAP's QueryMaterialIn/ManageMaterialIn docs).
# unit_code is the default unit this app writes/expects on read for each -
# NOT a universal tenant guarantee, but matches SAP's own published examples
# and this tenant's one live-confirmed real value (NET_WT in KGM).
PHYSICAL_ATTRIBUTE_CODES = {
    "net_weight_kg": ("NET_WT", "KGM"),
    "gross_weight_kg": ("GROSS_WT", "KGM"),
    "net_volume_cm3": ("NET_VOL", "MTQ"),
    "gross_volume_cm3": ("GROSS_VOL", "MTQ"),
    "length_mm": ("DIM_LENGTH", "MTR"),
    "width_mm": ("BREADTH", "MTR"),
    "height_mm": ("HEIGHT", "MTR"),
}
_VOLUME_FIELDS = {"net_volume_cm3", "gross_volume_cm3"}
_LENGTH_FIELDS = {"length_mm", "width_mm", "height_mm"}


def _sap_unit_to_app_value(field: str, value: float, unit_code: str) -> float:
    """Converts a value SAP returned in its own unit_code into this app's
    display unit (cm3 for volume, mm for length/width/height, kg unchanged
    for weight) - only when unit_code matches the expected default (MTQ/
    MTR); otherwise returns the raw SAP value unchanged (no silent
    misrepresentation of an unexpected tenant unit)."""
    _, expected_unit = PHYSICAL_ATTRIBUTE_CODES[field]
    if unit_code != expected_unit:
        return value
    if field in _VOLUME_FIELDS:
        return value * 1_000_000  # m^3 -> cm^3
    if field in _LENGTH_FIELDS:
        return value * 1000  # m -> mm
    return value


def _app_value_to_sap_unit(field: str, value: float) -> float:
    """Inverse of _sap_unit_to_app_value - converts this app's display
    value into the SAP default unit_code for that field before writing."""
    if field in _VOLUME_FIELDS:
        return value / 1_000_000  # cm^3 -> m^3
    if field in _LENGTH_FIELDS:
        return value / 1000  # mm -> m
    return value


def _tag_re(tag: str):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


_DOCUMENT_RE = re.compile(r"<(?:\w+:)?Document(?:\s[^>]*)?>(.*?)</(?:\w+:)?Document>", re.S)
_QUANTITY_CHARACTERISTIC_RE = re.compile(r"<QuantityCharacteristic(?:\s[^>]*)?>(.*?)</QuantityCharacteristic>", re.S)

# SAP ByDesign's standard Attachment Type codeset - only the ones actually
# seen on this tenant so far are listed; anything else falls back to a
# generic "Type {code}" label rather than showing a raw unlabeled number.
ATTACHMENT_TYPE_LABELS = {
    "10001": "Standard Attachment",
    "10011": "Product Image",
    "10015": "Technical Drawing",
    "10018": "Product Specification",
    "10043": "Details for Supplier",
}


class SAPMaterialClient:
    def __init__(self, endpoint: str, username: str, password: str, manage_endpoint: str = None):
        self.endpoint = endpoint
        self.manage_endpoint = manage_endpoint
        self.auth = HTTPBasicAuth(username, password)

    @staticmethod
    def _request_xml(internal_id: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <glob:MaterialByElementsQuery_sync>
   <MaterialSelectionByElements><SelectionByInternalID>
    <InclusionExclusionCode>I</InclusionExclusionCode>
    <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
    <LowerBoundaryInternalID>{internal_id}</LowerBoundaryInternalID>
    <UpperBoundaryInternalID/>
   </SelectionByInternalID></MaterialSelectionByElements>
   <ProcessingConditions>
    <QueryHitsMaximumNumberValue>1</QueryHitsMaximumNumberValue>
    <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
   </ProcessingConditions>
  </glob:MaterialByElementsQuery_sync>
 </soapenv:Body></soapenv:Envelope>"""

    @staticmethod
    def _physical_attributes_request_xml(internal_id: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <glob:MaterialByElementsQuery_sync>
   <MaterialSelectionByElements><SelectionByInternalID>
    <InclusionExclusionCode>I</InclusionExclusionCode>
    <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
    <LowerBoundaryInternalID>{internal_id}</LowerBoundaryInternalID>
    <UpperBoundaryInternalID/>
   </SelectionByInternalID></MaterialSelectionByElements>
   <ProcessingConditions>
    <QueryHitsMaximumNumberValue>1</QueryHitsMaximumNumberValue>
    <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
   </ProcessingConditions>
   <RequestedElements materialTransmissionRequestCode="2">
    <Material quantityCharacteristicTransmissionRequestCode="2" />
   </RequestedElements>
  </glob:MaterialByElementsQuery_sync>
 </soapenv:Body></soapenv:Envelope>"""

    def get_physical_attributes(self, internal_id: str):
        """Live-reads the Material master's "UoM Characteristics" data
        (Net/Gross Weight, Net/Gross Volume, Length/Width/Height) via the
        already-authorized QueryMaterialIn service, requesting the
        QuantityCharacteristic node explicitly (empty by default - live-
        confirmed 18 Aug 2026 across all 11,092 materials in this tenant,
        only 2 have any value set at all, so an empty result here is normal,
        not an error). Returns {"uuid", "change_state_id", "attributes":
        {field_name: {"value": float (app units), "sap_value": float,
        "sap_unit": str}}} or None if SAP has no material with that ID."""
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    data=self._physical_attributes_request_xml(internal_id).encode("utf-8"),
                    auth=self.auth,
                    headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPMaterialError(f"SAP is currently unreachable - please retry ({e})") from e
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            if "Authorization role missing" in faultstring:
                raise SAPMaterialAuthError(faultstring)
            raise SAPMaterialError(faultstring)

        material_match = re.search(r"<(?:\w+:)?Material(?:\s[^>]*)?>(.*?)</(?:\w+:)?Material>", xml, re.S)
        if not material_match:
            return None
        block = material_match.group(1)
        if _first_tag(block, "InternalID") != internal_id:
            return None

        by_type_code = {}
        for qc_match in _QUANTITY_CHARACTERISTIC_RE.finditer(block):
            qc_block = qc_match.group(1)
            type_code = _first_tag(qc_block, "CharacteristicQuantityTypeCode")
            value_str = _first_tag(qc_block, "CharacteristicQuantity")
            unit_match = re.search(r'<CharacteristicQuantity\s+unitCode="([^"]*)"', qc_block)
            if not type_code or value_str is None or not unit_match:
                continue
            by_type_code[type_code] = {"value": float(value_str), "unit": unit_match.group(1)}

        attributes = {}
        for field, (type_code, _) in PHYSICAL_ATTRIBUTE_CODES.items():
            hit = by_type_code.get(type_code)
            if hit:
                attributes[field] = {
                    "value": _sap_unit_to_app_value(field, hit["value"], hit["unit"]),
                    "sap_value": hit["value"],
                    "sap_unit": hit["unit"],
                }
        return {
            "uuid": _first_tag(block, "UUID"),
            "change_state_id": _first_tag(block, "ChangeStateID"),
            "attributes": attributes,
            "existing_type_codes": set(by_type_code.keys()),
        }

    def push_physical_attributes(self, internal_id: str, material_uuid: str, change_state_id: str,
                                  existing_type_codes: set, values: dict):
        """Writes physical attributes (values keyed by the PHYSICAL_ATTRIBUTE_CODES
        field names, in app units) to SAP via ManageMaterialIn's MaintainBundle_V1.
        Uses actionCode="01" (create) for a characteristic SAP doesn't have yet for
        this material, "02" (update) for one it does - required by SAP (sending "02"
        for a missing line is a business error per SAP's own docs). Raises
        SAPMaterialWriteNotConfiguredError if the tenant has no Communication
        Arrangement for this service yet (live-confirmed 18 Aug 2026 fault text)."""
        if not self.manage_endpoint:
            raise SAPMaterialWriteNotConfiguredError("SAP_SOAP_MATERIAL_MANAGE_ENDPOINT is not configured")
        lines = []
        for field, value in values.items():
            if value is None or field not in PHYSICAL_ATTRIBUTE_CODES:
                continue
            type_code, unit_code = PHYSICAL_ATTRIBUTE_CODES[field]
            action = "02" if type_code in existing_type_codes else "01"
            sap_value = _app_value_to_sap_unit(field, value)
            lines.append(
                f'<QuantityCharacteristic actionCode="{action}">'
                f'<QuantityMeasureUnitCode>EA</QuantityMeasureUnitCode>'
                f'<CharacteristicQuantity unitCode="{unit_code}">{sap_value}</CharacteristicQuantity>'
                f'<CharacteristicQuantityTypeCode>{type_code}</CharacteristicQuantityTypeCode>'
                f'</QuantityCharacteristic>'
            )
        if not lines:
            raise SAPMaterialError("Nothing to push - set at least one physical attribute first")

        xml_req = f"""<?xml version="1.0" encoding="UTF-8"?>
<MaterialBundleMaintainRequest_sync_V1 xmlns="http://sap.com/xi/A1S/Global">
 <BasicMessageHeader><ID>{uuid.uuid4().hex}</ID></BasicMessageHeader>
 <Material actionCode="02" quantityCharacteristicListCompleteTransmissionIndicator="false">
  <ChangeStateID>{change_state_id}</ChangeStateID>
  <InternalID>{internal_id}</InternalID>
  <UUID>{material_uuid}</UUID>
  {''.join(lines)}
 </Material>
</MaterialBundleMaintainRequest_sync_V1>"""
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.manage_endpoint,
                    data=xml_req.encode("utf-8"),
                    auth=self.auth,
                    headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPMaterialError(f"SAP is currently unreachable - please retry ({e})") from e
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            if "Web service processing error" in faultstring:
                raise SAPMaterialWriteNotConfiguredError(faultstring)
            if "Authorization role missing" in faultstring:
                raise SAPMaterialAuthError(faultstring)
            raise SAPMaterialError(faultstring)
        return True

    def resolve_material_info(self, internal_id: str):
        """Returns {"uuid": str|None, "drawing_url": str|None, "comments":
        list[dict]} for a Material's InternalID (business ID, e.g.
        'SPC5WM'). Both drawing_url and comments come from the same
        Material master AttachmentFolder.Document node(s) - SAP ByDesign
        lets a Document entry carry an external web link
        (ExternalLinkWebURI, this tenant uses it to point at drawings/
        documentation hosted on a separate shared-drive portal) AND a
        free-text Description - this is exactly the "Comment" field shown
        against an attachment in the SAP ByDesign UI (e.g. an ECR note like
        "ECR No. 83 raised to correct the Marked identification of Left
        and Right Arm."), confirmed live on 11 Aug 2026. Not every
        Document has a Description, and a Material can have more than one
        Document - `comments` only includes the ones that actually have a
        non-empty Description, each as {"title", "type_code",
        "type_label", "comment"}. Returns all-None/empty if SAP has no
        material with that exact InternalID. Raises SAPMaterialAuthError
        if the technical user isn't authorized for this service, or
        SAPMaterialError for any other SOAP fault/HTTP error."""
        with sap_semaphore:
            resp = requests.post(
                self.endpoint,
                data=self._request_xml(internal_id).encode("utf-8"),
                auth=self.auth,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                timeout=45,
            )
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            if "Authorization role missing" in faultstring:
                raise SAPMaterialAuthError(faultstring)
            raise SAPMaterialError(faultstring)

        material_match = re.search(r"<(?:\w+:)?Material(?:\s[^>]*)?>(.*?)</(?:\w+:)?Material>", xml, re.S)
        if not material_match:
            return {"uuid": None, "drawing_url": None, "comments": []}
        block = material_match.group(1)
        returned_id = _first_tag(block, "InternalID")
        if returned_id != internal_id:
            return {"uuid": None, "drawing_url": None, "comments": []}
        material_uuid = _first_tag(block, "UUID")

        drawing_url = None
        comments = []
        for doc_match in _DOCUMENT_RE.finditer(block):
            doc_block = doc_match.group(1)
            if drawing_url is None:
                drawing_url = _first_tag(doc_block, "ExternalLinkWebURI")
            comment_text = _first_tag(doc_block, "Description")
            if comment_text:
                type_code = _first_tag(doc_block, "TypeCode")
                comments.append({
                    "title": _first_tag(doc_block, "AlternativeName") or _first_tag(doc_block, "Name"),
                    "type_code": type_code,
                    "type_label": ATTACHMENT_TYPE_LABELS.get(type_code, f"Type {type_code}" if type_code else None),
                    "comment": comment_text,
                })
        return {"uuid": material_uuid, "drawing_url": drawing_url, "comments": comments}

    def resolve_uuid(self, internal_id: str):
        """Returns just the material's UUID (str), or None - thin wrapper
        over resolve_material_info() kept for existing callers that only
        need the UUID (e.g. the Inventory page's deep backfill)."""
        return self.resolve_material_info(internal_id)["uuid"]
