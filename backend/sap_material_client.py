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


_DOCUMENT_RE = re.compile(r"<(?:\w+:)?Document(?:\s[^>]*)?>(.*?)</(?:\w+:)?Document>", re.S)
_QUANTITY_CONVERSION_RE = re.compile(r"<(?:\w+:)?QuantityConversion(?:\s[^>]*)?>(.*?)</(?:\w+:)?QuantityConversion>", re.S)


def _quantity_with_unit(xml_block: str, tag: str):
    """Extracts (unit_code, value) from a `<Tag unitCode="...">123.0</Tag>`
    element, or None if `tag` isn't present in `xml_block`."""
    m = re.search(rf'<(?:\w+:)?{tag}\s+unitCode="([^"]*)"[^>]*>([^<]*)</(?:\w+:)?{tag}>', xml_block)
    return (m.group(1), float(m.group(2))) if m else None

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
        """Returns {"uuid": str|None, "drawing_url": str|None, "comments":
        list[dict], "description": str|None, "base_unit": str|None,
        "life_cycle_status_code": str|None} for a Material's InternalID
        (business ID, e.g. 'SPC5WM'). Both drawing_url and comments come
        from the same Material master AttachmentFolder.Document node(s) -
        SAP ByDesign lets a Document entry carry an external web link
        (ExternalLinkWebURI, this tenant uses it to point at drawings/
        documentation hosted on a separate shared-drive portal) AND a
        free-text Description - this is exactly the "Comment" field shown
        against an attachment in the SAP ByDesign UI (e.g. an ECR note like
        "ECR No. 83 raised to correct the Marked identification of Left
        and Right Arm."), confirmed live on 11 Aug 2026. Not every
        Document has a Description, and a Material can have more than one
        Document - `comments` only includes the ones that actually have a
        non-empty Description, each as {"title", "type_code",
        "type_label", "comment"}. `description`/`base_unit` (Sep 9 2026,
        added for the PR-driven PO Creation live-fallback match - see
        server.py's pr-lookup endpoint) come from the Material's own
        <Description><Description languageCode="EN">...</Description>
        </Description> (note the nested tag - NOT the same "Description"
        tag used per-Document above, which is why this reads the material
        block directly rather than reusing _first_tag on the whole block
        naively) and <BaseMeasureUnitCode>. Returns all-None/empty if SAP
        has no material with that exact InternalID. Raises
        SAPMaterialAuthError if the technical user isn't authorized for
        this service, or SAPMaterialError for any other SOAP fault/HTTP
        error."""
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

        empty = {"uuid": None, "drawing_url": None, "comments": [], "description": None, "base_unit": None, "life_cycle_status_code": None, "product_category_id": None, "active_sites": []}
        material_match = re.search(r"<(?:\w+:)?Material(?:\s[^>]*)?>(.*?)</(?:\w+:)?Material>", xml, re.S)
        if not material_match:
            return empty
        block = material_match.group(1)
        returned_id = _first_tag(block, "InternalID")
        if returned_id != internal_id:
            return empty
        material_uuid = _first_tag(block, "UUID")
        base_unit = _first_tag(block, "BaseMeasureUnitCode")
        life_cycle_status_code = None
        purchasing_match = re.search(r"<Purchasing>(.*?)</Purchasing>", block, re.S)
        if purchasing_match:
            life_cycle_status_code = _first_tag(purchasing_match.group(1), "LifeCycleStatusCode")
        description = None
        desc_match = re.search(r"<Description>\s*<Description[^>]*>(.*?)</Description>\s*</Description>", block, re.S)
        if desc_match:
            description = desc_match.group(1).strip() or None

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
        return {
            "uuid": material_uuid, "drawing_url": drawing_url, "comments": comments,
            "description": description, "base_unit": base_unit, "life_cycle_status_code": life_cycle_status_code,
            # Sep 11 2026, user's explicit ask (standalone "Activate Material
            # at Site" page) - ProductCategoryID + which sites are truly
            # ACTIVE (not just present) at this material, so the page can
            # show "already active at: P1, P2..." before the user picks a
            # target site.
            #
            # BUG FIX (same day, user report on 6800-004473): a SupplyPlanning
            # node can exist at a site while its own LifeCycleStatusCode is
            # still "1" (In Preparation, SAP's yellow-warning Logistics tab
            # state) rather than "2" (Active, green check) - the previous
            # version counted ANY SupplyPlanning node as "active", which
            # wrongly reported P2/P4 as already-active for 6800-004473 when
            # SAP's own Logistics tab clearly showed them "In Preparation".
            # Live-confirmed the fix: SAP returns P1/P3/P9 with code "2" and
            # P2/P4/P6/P7 with code "1" for this exact material - now only
            # code "2" sites are reported as active.
            "product_category_id": _first_tag(block, "ProductCategoryID"),
            "active_sites": sorted({
                _first_tag(sp_block.group(1), "SupplyPlanningAreaID")
                for sp_block in re.finditer(r"<(?:\w+:)?SupplyPlanning(?:\s[^>]*)?>(.*?)</(?:\w+:)?SupplyPlanning>", block, re.S)
                if _first_tag(sp_block.group(1), "SupplyPlanningAreaID") and _first_tag(sp_block.group(1), "LifeCycleStatusCode") == "2"
            }),
        }

    def resolve_uuid(self, internal_id: str):
        """Returns just the material's UUID (str), or None - thin wrapper
        over resolve_material_info() kept for existing callers that only
        need the UUID (e.g. the Inventory page's deep backfill)."""
        return self.resolve_material_info(internal_id)["uuid"]

    def get_uom_info(self, internal_id: str):
        """Sep 9 2026, user's explicit ask: "fetch secondary unit of the
        item while creating PO" (e.g. 6550-002047, 1 Packet = 100 EA
        maintained in SAP's Material master "Quantity Conversions" grid,
        General tab). Returns {"base_unit": str|None, "alternate_units":
        [{"unit_code", "unit_qty", "base_unit_code", "base_qty"}]}, or
        None if SAP has no material with that exact InternalID. A
        material can have zero, one, or several alternate units
        configured - each becomes its own dict in the list. Raises
        SAPMaterialAuthError/SAPMaterialError same as
        resolve_material_info()."""
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
            return None
        block = material_match.group(1)
        if _first_tag(block, "InternalID") != internal_id:
            return None

        base_unit = _first_tag(block, "BaseMeasureUnitCode")
        alternate_units = []
        for conv_match in _QUANTITY_CONVERSION_RE.finditer(block):
            conv_block = conv_match.group(1)
            unit_qty = _quantity_with_unit(conv_block, "Quantity")
            base_qty = _quantity_with_unit(conv_block, "CorrespondingQuantity")
            if unit_qty and base_qty:
                alternate_units.append({
                    "unit_code": unit_qty[0], "unit_qty": unit_qty[1],
                    "base_unit_code": base_qty[0], "base_qty": base_qty[1],
                })
        return {"base_unit": base_unit, "alternate_units": alternate_units}

    def get_existing_procurement_type_code(self, internal_id: str):
        """Sep 5 2026, real incident (STO-135, SPLICE @ site P8): the
        "Activate this site" fix (sap_material_create_client.activate_site)
        always hardcoded ProcurementTypeCode=2 ("External procurement") for
        every new site, which works for raw materials bought externally
        (e.g. FLAT-BK50/FLAT-BK21, code 2 at EVERY site), but SAP flat-out
        rejects it - with the misleading "Supply planning ID P8; does not
        exist" - for in-house-manufactured Semi-Finished Goods, which need
        code 1 ("In-house production") at manufacturing sites (confirmed
        live: SPLICE/AB24/P41551 - all ProductCategoryID "SFG" - use code 1
        at P1/P2/P4/P6/P7/P8, only code 2 at the P1W/P5 trading sites).
        Rather than guess from ProductCategoryID (only 2 values seen so
        far, not exhaustively confirmed), this reads the material's OWN
        existing SupplyPlanning entries at any OTHER site and reuses
        whichever ProcurementTypeCode already appears most often - the
        material's own established sourcing strategy is the one ground
        truth that generalizes to any future material/site combination.
        Returns None if this material has no existing SupplyPlanning
        entries at all (caller falls back to the old default "2")."""
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
            return None
        codes = re.findall(r"<(?:\w+:)?SupplyPlanning(?:\s[^>]*)?>.*?<ProcurementTypeCode>([^<]*)</ProcurementTypeCode>.*?</(?:\w+:)?SupplyPlanning>", xml, re.S)
        if not codes:
            return None
        return max(set(codes), key=codes.count)
