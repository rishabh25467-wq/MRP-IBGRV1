"""SAP Business ByDesign QuerySupplierIn SOAP client - reads the Supplier
master list (Internal ID, UUID, formatted name, contact person, email,
phone) so the local Suppliers page can be populated from real SAP data
instead of manual typing.

Status as of this session: the exact Communication Arrangement/Scenario for
"Query Supplier" has NOT been confirmed active in this tenant - a live probe
against the guessed endpoint returned a generic SOAP processing fault (not
the specific "Authorization role missing" fault seen previously with
QueryMaterialIn before ITS arrangement was set up), which most likely means
no inbound service is currently routed at this path at all. See
/app/SAP_SUPPLIER_SYNC_AUTHORIZATION_REQUEST.md for the exact setup steps
needed from the SAP admin - the user has self-resolved two similar setups
before (Production BOM Query, materialquery) without SAP support access."""
import re

import requests
from requests.auth import HTTPBasicAuth


class SAPSupplierError(Exception):
    pass


class SAPSupplierAuthError(SAPSupplierError):
    """SAP recognizes the service but the technical user lacks the
    authorization role for it - the Communication Arrangement exists but
    needs the QuerySupplierIn operation added to its business role."""
    pass


class SAPSupplierNotConfiguredError(SAPSupplierError):
    """The service endpoint isn't routed to anything in this tenant at all
    - no Communication Arrangement/Scenario for Query Supplier has been
    created yet (distinct from an auth-role gap on an existing one)."""
    pass


def _tag_re(tag: str):
    return re.compile(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", re.S)


def _first_tag(xml: str, tag: str):
    m = _tag_re(tag).search(xml)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _all_blocks(xml: str, tag: str):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


class SAPSupplierClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    @staticmethod
    def _request_xml(max_hits: int) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/><soapenv:Body>
  <glob:SupplierByElementsQuery_sync>
   <SupplierSelectionByElements>
    <SelectionByLifeCycleStatusCode>2</SelectionByLifeCycleStatusCode>
   </SupplierSelectionByElements>
   <ProcessingConditions>
    <QueryHitsMaximumNumberValue>{max_hits}</QueryHitsMaximumNumberValue>
    <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
   </ProcessingConditions>
  </glob:SupplierByElementsQuery_sync>
 </soapenv:Body></soapenv:Envelope>"""

    def list_suppliers(self, max_hits: int = 500) -> list:
        """Returns a list of dicts: internal_id, uuid, name, email, phone.
        Raises SAPSupplierAuthError / SAPSupplierNotConfiguredError /
        SAPSupplierError on failure - callers should surface these as an
        actionable message, not a silent empty list."""
        try:
            resp = requests.post(
                self.endpoint,
                data=self._request_xml(max_hits).encode("utf-8"),
                auth=self.auth,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                timeout=45,
            )
        except requests.exceptions.RequestException as e:
            raise SAPSupplierError(f"Could not reach SAP: {e}")

        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            if "Authorization role missing" in faultstring:
                raise SAPSupplierAuthError(faultstring)
            if "Web service processing error" in faultstring:
                raise SAPSupplierNotConfiguredError(faultstring)
            raise SAPSupplierError(faultstring)

        results = []
        for block in _all_blocks(xml, "Supplier"):
            internal_id = _first_tag(block, "InternalID")
            if not internal_id:
                continue
            results.append({
                "internal_id": internal_id,
                "uuid": _first_tag(block, "UUID"),
                "name": _first_tag(block, "BusinessPartnerFormattedName") or _first_tag(block, "FormattedName") or internal_id,
                "email": _first_tag(block, "URI") or _first_tag(block, "EMailURI"),
                "phone": _first_tag(block, "CompleteNumberDescription") or _first_tag(block, "NormalisedNumberDescription"),
            })
        return results
