"""SAP Business ByDesign ManageProductionProposalIn/CreateBundle SOAP
client - creates a Production Proposal (Material/Site/Quantity/Date),
which SAP's planning run then explodes into a Production Request and
Production Order. This is SAP's only supported way to create a new
production order via web service (confirmed via SAP's own docs/advisors -
there is no direct "create production order" API)."""
import re
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageProductionProposalIn/CreateBundleRequest"


class SAPProductionProposalError(Exception):
    pass


def _first_tag(xml: str, tag: str):
    m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _all_blocks(xml: str, tag: str):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


class SAPProductionProposalClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def create_proposal(self, material_id: str, site_id: str, quantity: float, unit_code: str, availability_datetime: datetime = None) -> dict:
        """Creates one Production Proposal. Returns {"production_proposal_id": str}.
        `site_id` is used as the Supply Planning Area ID (matches Site ID
        for this tenant's simple single-SPA-per-site setup)."""
        availability_datetime = availability_datetime or datetime.now(timezone.utc)
        avail_str = availability_datetime.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:CreateProductionProposal xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <CreateProductionPlanningOrder>
        <SequenceNumber>1</SequenceNumber>
        <SupplyPlanningAreaID>{site_id}</SupplyPlanningAreaID>
        <MaterialID>{material_id}</MaterialID>
        <MaterialAvailabilityDateTime timeZoneCode="UTC">{avail_str}</MaterialAvailabilityDateTime>
        <Quantity unitCode="{unit_code}">{quantity}</Quantity>
        <QuantityTypeCode>{unit_code}</QuantityTypeCode>
      </CreateProductionPlanningOrder>
    </n0:CreateProductionProposal>
  </soapenv:Body>
</soapenv:Envelope>"""

        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    data=body.encode("utf-8"),
                    auth=self.auth,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPProductionProposalError(f"Could not reach SAP: {e}")

        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultText") or _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            details = [d for d in (_first_tag(b, "text") for b in _all_blocks(xml, "faultDetail")) if d]
            raise SAPProductionProposalError(faultstring + ((" | " + "; ".join(details)) if details else ""))

        proposal_id = _first_tag(xml, "ProductionProposalID")
        log_note = _first_tag(xml, "Log")
        if not proposal_id:
            raise SAPProductionProposalError(log_note or "SAP did not return a Production Proposal ID")
        return {"production_proposal_id": proposal_id, "log": log_note}
