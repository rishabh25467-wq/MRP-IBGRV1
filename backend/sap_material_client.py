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


def _tag_re(tag: str):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


class SAPMaterialClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
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

    def resolve_material_info(self, internal_id: str):
        """Returns {"uuid": str|None, "drawing_url": str|None} for a
        Material's InternalID (business ID, e.g. 'SPC5WM'). The drawing_url
        comes from the Material master's own AttachmentFolder.Document -
        SAP ByDesign lets a Document entry be either an uploaded file OR a
        plain external web link (ExternalLinkWebURI); this tenant uses the
        latter to point at drawings/documentation hosted on a separate
        shared-drive portal (e.g. 'https://rampgroup.net/Documents.aspx?
        mid=SPC5WM') - confirmed live, NOT every material has one. Returns
        {"uuid": None, "drawing_url": None} if SAP has no material with
        that exact InternalID. Raises SAPMaterialAuthError if the technical
        user isn't authorized for this service, or SAPMaterialError for any
        other SOAP fault/HTTP error."""
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
            return {"uuid": None, "drawing_url": None}
        block = material_match.group(1)
        returned_id = _first_tag(block, "InternalID")
        if returned_id != internal_id:
            return {"uuid": None, "drawing_url": None}
        material_uuid = _first_tag(block, "UUID")
        drawing_url = _first_tag(block, "ExternalLinkWebURI")
        return {"uuid": material_uuid, "drawing_url": drawing_url}

    def resolve_uuid(self, internal_id: str):
        """Returns just the material's UUID (str), or None - thin wrapper
        over resolve_material_info() kept for existing callers that only
        need the UUID (e.g. the Inventory page's deep backfill)."""
        return self.resolve_material_info(internal_id)["uuid"]
