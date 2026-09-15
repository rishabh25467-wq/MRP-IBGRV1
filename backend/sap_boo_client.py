"""SAP Business ByDesign "Read Production Bill of Operations" - standard,
released A1S SOAP web service (ManageProductionBillofOperationsIn ->
ReadProductionBillofOperations), activated via a Communication Arrangement
by the user's SAP admin (Aug 2026, user's explicit ask - "I need Reporting
Point Description ex: BLANK+PUNCH, FLAT-LANCER").

Confirmed live: a Production Lot's ReportingPoint code (e.g. "RP_10") is
just a technical ID - its human-readable name ("BLANK+PUCNCH") only lives
on the Bill of Operations' own "Marker" elements (ElementTypeCode=5),
looked up by BillOfOperationsID (obtained separately via
sap_production_model_client.SAPProductionModelBomClient, keyed off the
Production Model used to create the order)."""
import html
import re

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

MARKER_ELEMENT_TYPE_CODE = "5"


class SAPBooError(Exception):
    pass


def _tag_re(tag: str):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else None


def _all_blocks(xml: str, tag: str):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


class SAPBooClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def get_marker_element_descriptions(self, bill_of_operations_id: str) -> dict:
        """Returns {ElementID: ElementDescription} for every Marker
        element (Reporting Point) of the given Bill of Operations - e.g.
        {"RP_10": "BLANK+PUCNCH", "RP_20": "FLAT_LANCER", "END": "BENDING"}."""
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
 <soapenv:Body>
  <n0:ProductionBillOfOperationReadByID_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
   <ProductionBillOfOperation>
    <ProductionBillOfOperationID>{bill_of_operations_id}</ProductionBillOfOperationID>
   </ProductionBillOfOperation>
   <RequestedElements ProductionBillOfOperationTransmissionRequestCode="1">
    <ProductionBillOfOperation BillOfOperationTransmissionRequestCode="1" OpeartionsTransmissionRequestCode="1" BillOfOperationPlanningViewTransmissionRequestCode="1" HierarchicalViewElementTransmissionRequestCode="1"/>
   </RequestedElements>
  </n0:ProductionBillOfOperationReadByID_sync>
 </soapenv:Body>
</soapenv:Envelope>"""
        headers = {"Content-Type": "text/xml; charset=UTF-8", "Accept": "text/xml"}
        try:
            with sap_semaphore:
                resp = requests.post(self.endpoint, data=body.encode("utf-8"), headers=headers, auth=self.auth, timeout=(8, 45))
        except requests.exceptions.RequestException as e:
            raise SAPBooError(str(e))
        xml = resp.text
        if resp.status_code == 401:
            raise SAPBooError("SAP SOAP authentication failed. Check communication arrangement credentials.")
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            fault_text = _first_tag(xml, "faultText") or _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            raise SAPBooError(fault_text)

        descriptions = {}
        for el_block in _all_blocks(xml, "Elements"):
            if _first_tag(el_block, "ElementTypeCode") != MARKER_ELEMENT_TYPE_CODE:
                continue
            element_id = _first_tag(el_block, "ElementID")
            description = _first_tag(el_block, "ElementDescription")
            if element_id and description:
                descriptions[element_id] = description

        # Fallback for Marker/Reporting Point elements with NO description
        # of their own in SAP (real incident, Sep 2026: RP_10/END both had
        # a blank <ElementDescription/> even though the Operation right
        # before each one - "BLANK"/"BENDING" - DID have a real process
        # name). Every Reporting Point is the EndElementID of exactly one
        # <PlanningOperations> phase, whose nested <ExecutionOperation>
        # carries that preceding Operation's ID + description - use that
        # instead so staff see e.g. "OP_10 - BLANK" rather than "RP_10".
        for po_block in _all_blocks(xml, "PlanningOperations"):
            end_element_id = _first_tag(po_block, "EndElementID")
            if not end_element_id or descriptions.get(end_element_id):
                continue
            exec_match = re.search(
                r"<(?:\w+:)?ExecutionOperation(?:\s[^>]*)?>(.*?)</(?:\w+:)?ExecutionOperation>", po_block, re.S,
            )
            if not exec_match:
                continue
            exec_block = exec_match.group(1)
            op_id = _first_tag(exec_block, "OperationID")
            op_desc = _first_tag(exec_block, "OperationDescription")
            if op_id and op_desc:
                descriptions[end_element_id] = f"{op_id} - {op_desc}"
            elif op_desc:
                descriptions[end_element_id] = op_desc
        return descriptions
