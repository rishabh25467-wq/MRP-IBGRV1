"""SAP Business ByDesign ManageMaterialCostEstimateRunDataBundle SOAP client
- triggers a real Cost Estimate Run for a single Material (by UUID), so a
sub-assembly that only shows a rolled-up (frontend-summed) cost in BOM
Explorer can get an actual SAP-calculated Standard Cost on demand.

WSDL note: the schema's own field for the upper-bound Product Key is
misspelled `ProductKeyHign` (not a typo on our side - present verbatim in
the WSDL SAP generated for this tenant). We avoid that whole element by
selecting the material purely by UUID (`UUIDLow`) instead of ProductKey,
which has no such issue.

CompanyID/SetOfBooksID are tenant-wide config values (SAP_COMPANY_ID /
SAP_SET_OF_BOOKS_ID in .env), not per-item.
"""
import re
from datetime import date

import requests
from requests.auth import HTTPBasicAuth

SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageMaterialCostEstimateRunDataBundle/ManageMaterialCostEstimateMaintainBundleRequest"


class SAPCostEstimateError(Exception):
    """Raised for a SOAP fault returned by SAP itself. `transaction_id`
    (when present) is the fault's own reference ID - hand it to the SAP
    Basis/Admin team so they can pull the actual root cause from the
    provider-side web service error log (not visible to us at all)."""

    def __init__(self, message: str, transaction_id: str = None):
        super().__init__(message)
        self.transaction_id = transaction_id


def _first_tag(xml: str, tag: str):
    m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


class SAPCostEstimateClient:
    def __init__(self, endpoint: str, username: str, password: str, company_id: str, set_of_books_id: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)
        self.company_id = company_id
        self.set_of_books_id = set_of_books_id

    def run_cost_estimate(self, material_uuid: str, description: str = None) -> dict:
        """Submits an immediate, active Cost Estimate Run scoped to a single
        Material UUID. Returns {"run_id": str, "uuid": str} on success.
        Raises SAPCostEstimateError on any SOAP fault/HTTP error."""
        today = date.today().isoformat()
        desc = (description or f"Emergent App Cost Estimate {material_uuid}")[:255]
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n1:MaterialCostEstimateRunRequest xmlns:n1="http://sap.com/xi/SAPGlobal20/Global">
      <BasicMessageHeader/>
      <MaterialCostEstimateRunRequest>
        <CompanyID>{self.company_id}</CompanyID>
        <ValidityStartDate>{today}</ValidityStartDate>
        <Description>{desc}</Description>
        <SelectionByMaterial>
          <InclusionExclusionCode>I</InclusionExclusionCode>
          <MDRO_IntervalBoundaryTypeCode>1</MDRO_IntervalBoundaryTypeCode>
          <UUIDLow>{material_uuid}</UUIDLow>
        </SelectionByMaterial>
        <SetOfBooksID>{self.set_of_books_id}</SetOfBooksID>
        <ActiveIndicator>true</ActiveIndicator>
        <ScheduleImmediatelyIndicator>true</ScheduleImmediatelyIndicator>
      </MaterialCostEstimateRunRequest>
    </n1:MaterialCostEstimateRunRequest>
  </soapenv:Body>
</soapenv:Envelope>"""

        try:
            resp = requests.post(
                self.endpoint,
                data=body.encode("utf-8"),
                auth=self.auth,
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
                timeout=45,
            )
        except requests.exceptions.RequestException as e:
            raise SAPCostEstimateError(f"Could not reach SAP: {e}")

        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            txn_match = re.search(r"Transaction ID ([A-F0-9]+)", faultstring)
            raise SAPCostEstimateError(faultstring, transaction_id=txn_match.group(1) if txn_match else None)

        run_id = _first_tag(xml, "BusinessTransactionDocumentID")
        run_uuid = _first_tag(xml, "UUID")
        return {"run_id": run_id, "uuid": run_uuid}
