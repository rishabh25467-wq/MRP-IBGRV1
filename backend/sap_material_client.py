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
import os
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


class SAPMaterialFieldNotConfiguredError(SAPMaterialError):
    """Raised when a custom field (Net Weight / Surface Area) isn't linked
    to the QueryMaterialIn/ManageMaterialIn web service yet on the SAP side
    (live-confirmed 18 Aug 2026: the SAP admin created "Item Net Weight"
    and "Surface Area(Sq.Inch)" as custom fields directly on the Material's
    General tab, e.g. Material 5989825-2.1 has Surface Area = 255 in the
    SAP UI, but NEITHER field appears at all in the QueryMaterialIn SOAP
    response - a custom field must be explicitly linked to a specific web
    service via Key User Tools > adaptation mode > Further Usage >
    Services > Add Field before it's exposed there, see
    SAP_MATERIAL_FIELD_SETUP_REQUEST.md)."""
    pass


class SAPMaterialWriteNotConfiguredError(SAPMaterialError):
    """Raised when the write-back service (ManageMaterialIn) has no
    Communication Arrangement/Scenario set up on the tenant at all yet -
    live-confirmed (18 Aug 2026) this shows up as a generic 'Web service
    processing error' fault, NOT the specific 'Authorization role missing'
    fault QueryMaterialIn showed before ITS own arrangement existed (same
    distinction already used in sap_supplier_client.py)."""
    pass


# Net Weight and Surface Area are CUSTOM extension fields added by the SAP
# admin directly on the Material's "General" tab (NOT SAP's standard
# QuantityCharacteristic/"UoM Characteristics" node - live-confirmed 18 Aug
# 2026 that tab is unpopulated tenant-wide). Custom fields show up in the
# SOAP response under an arbitrary namespace prefix once (and only once)
# the SAP admin links them to QueryMaterialIn/ManageMaterialIn - until then
# these env vars are blank and the field is simply omitted (not an error).
PHYSICAL_FIELD_CONFIG = {
    "net_weight_kg": {
        "tag": os.environ.get("SAP_MATERIAL_NET_WEIGHT_FIELD_TAG"),
        "ns": os.environ.get("SAP_MATERIAL_NET_WEIGHT_FIELD_NS"),
    },
    "surface_area_sqin": {
        "tag": os.environ.get("SAP_MATERIAL_SURFACE_AREA_FIELD_TAG"),
        "ns": os.environ.get("SAP_MATERIAL_SURFACE_AREA_FIELD_NS"),
    },
}


def _tag_re(tag: str):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


_DOCUMENT_RE = re.compile(r"<(?:\w+:)?Document(?:\s[^>]*)?>(.*?)</(?:\w+:)?Document>", re.S)

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

    def get_physical_attributes(self, internal_id: str):
        """Live-reads the Material's custom "Item Net Weight" and "Surface
        Area(Sq.Inch)" fields (added by the SAP admin directly on the
        Material General tab - NOT SAP's standard "UoM Characteristics"
        node) via QueryMaterialIn. Returns None for a field whose
        SAP_MATERIAL_*_FIELD_TAG/_NS env vars aren't set yet (not linked to
        this web service by the SAP admin yet - see
        SAP_MATERIAL_FIELD_SETUP_REQUEST.md) - not an error. Returns
        {"uuid", "change_state_id", "attributes": {field_name: {"value":
        float, "unit": str|None}}} or None if SAP has no material with
        that ID."""
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    data=self._request_xml(internal_id).encode("utf-8"),
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

        attributes = {}
        for field, cfg in PHYSICAL_FIELD_CONFIG.items():
            tag = cfg["tag"]
            if not tag:
                continue
            m = _tag_re(tag).search(block)
            if not m:
                continue
            unit_match = re.search(rf'<(?:\w+:)?{tag}\s[^>]*unitCode="([^"]*)"', block)
            raw_value = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            try:
                attributes[field] = {"value": float(raw_value), "unit": unit_match.group(1) if unit_match else None}
            except ValueError:
                continue

        return {
            "uuid": _first_tag(block, "UUID"),
            "change_state_id": _first_tag(block, "ChangeStateID"),
            "attributes": attributes,
        }

    def push_physical_attributes(self, internal_id: str, material_uuid: str, change_state_id: str, values: dict):
        """Writes Net Weight / Surface Area (values keyed by
        PHYSICAL_FIELD_CONFIG's field names) to SAP's custom Material
        fields via ManageMaterialIn's MaintainBundle_V1 - simple scalar
        extension fields, no actionCode needed (unlike a collection node).
        Raises SAPMaterialFieldNotConfiguredError if a field's tag/ns env
        vars aren't set, or SAPMaterialWriteNotConfiguredError if the
        tenant has no Communication Arrangement for ManageMaterialIn at
        all yet (live-confirmed 18 Aug 2026 'Web service processing
        error' fault)."""
        if not self.manage_endpoint:
            raise SAPMaterialWriteNotConfiguredError("SAP_SOAP_MATERIAL_MANAGE_ENDPOINT is not configured")
        lines = []
        skipped_unconfigured = []
        for field, value in values.items():
            if value is None or field not in PHYSICAL_FIELD_CONFIG:
                continue
            cfg = PHYSICAL_FIELD_CONFIG[field]
            if not cfg["tag"] or not cfg["ns"]:
                skipped_unconfigured.append(field)
                continue
            lines.append(f'<n1:{cfg["tag"]} xmlns:n1="{cfg["ns"]}">{value}</n1:{cfg["tag"]}>')
        if not lines:
            if skipped_unconfigured:
                raise SAPMaterialFieldNotConfiguredError(
                    f"These fields aren't linked to ManageMaterialIn on the SAP side yet: {', '.join(skipped_unconfigured)}"
                )
            raise SAPMaterialError("Nothing to push - set Net Weight and/or Surface Area first")

        xml_req = f"""<?xml version="1.0" encoding="UTF-8"?>
<MaterialBundleMaintainRequest_sync_V1 xmlns="http://sap.com/xi/A1S/Global">
 <BasicMessageHeader><ID>{uuid.uuid4().hex}</ID></BasicMessageHeader>
 <Material actionCode="02">
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
