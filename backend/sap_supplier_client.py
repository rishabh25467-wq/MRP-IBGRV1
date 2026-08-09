"""SAP Business ByDesign QuerySupplierIn SOAP client - reads the Supplier
master list (Internal ID, UUID, formatted name, contact person, email,
phone) so the local Suppliers page can be populated from real SAP data
instead of manual typing.

Working as of 08 Aug 2026 - the "BusinessPartnerEmergent" Communication
Scenario/Arrangement (QuerySupplierIn / Find Suppliers operation) was
activated for the _EMERGENTBOM business user. Empirically confirmed: the
`SelectionByLifeCycleStatusCode` selection criterion must use the full
interval-boundary structure (InclusionExclusionCode/IntervalBoundaryTypeCode/
LowerBoundaryLifeCycleStatusCode) - a bare value tag causes SAP to reject
the whole request with a generic, unhelpful "Web service processing error"
fault (easy to mistake for "service not configured" - it isn't).

Contact person data (name + mobile, as seen on a Supplier's "Contacts" tab
in SAP's own UI) is NOT returned by default - it's an opt-in response
expansion via `<RequestedElements supplierTransmissionRequestCode="2">
<Supplier contactPersonTransmissionRequestCode="1"/></RequestedElements>`.
Without that, every supplier's Contact Person/phone show up blank in our
master list even though SAP has the data (researched + confirmed 09 Aug
2026)."""
import html
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
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else None


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
    <SelectionByLifeCycleStatusCode>
     <InclusionExclusionCode>I</InclusionExclusionCode>
     <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
     <LowerBoundaryLifeCycleStatusCode>2</LowerBoundaryLifeCycleStatusCode>
    </SelectionByLifeCycleStatusCode>
   </SupplierSelectionByElements>
   <ProcessingConditions>
    <QueryHitsMaximumNumberValue>{max_hits}</QueryHitsMaximumNumberValue>
    <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
   </ProcessingConditions>
   <RequestedElements supplierTransmissionRequestCode="2">
    <Supplier addressInformationTransmissionRequestCode="1" contactPersonTransmissionRequestCode="1"/>
   </RequestedElements>
  </glob:SupplierByElementsQuery_sync>
 </soapenv:Body></soapenv:Envelope>"""

    @staticmethod
    def _parse_contact_person(block: str):
        """Picks the default contact (DefaultContactPersonIndicator=true) if
        there are several, else the first one. Returns (full_name, phone,
        email) - phone prefers the mobile-flagged WorkplaceTelephone, falls
        back to the first non-mobile one."""
        contact_blocks = _all_blocks(block, "ContactPerson")
        if not contact_blocks:
            return None, None, None
        default = next(
            (c for c in contact_blocks if (_first_tag(c, "DefaultContactPersonIndicator") or "").lower() == "true"),
            contact_blocks[0],
        )
        given = _first_tag(default, "GivenName")
        family = _first_tag(default, "FamilyName")
        full_name = " ".join(p for p in [given, family] if p) or None
        email = _first_tag(default, "WorkplaceEMailURI")
        phone = None
        mobile_phone = None
        for tel_block in _all_blocks(default, "WorkplaceTelephone"):
            number = _first_tag(tel_block, "FormattedNumberDescription")
            if not number:
                continue
            if (_first_tag(tel_block, "MobilePhoneNumberIndicator") or "").lower() == "true":
                mobile_phone = mobile_phone or number
            else:
                phone = phone or number
        return full_name, mobile_phone or phone, email

    def list_suppliers(self, max_hits: int = 6000) -> list:
        """Returns a list of dicts: internal_id, uuid, name, email, phone,
        contact_person. Raises SAPSupplierAuthError /
        SAPSupplierNotConfiguredError / SAPSupplierError on failure -
        callers should surface these as an actionable message, not a
        silent empty list."""
        try:
            resp = requests.post(
                self.endpoint,
                data=self._request_xml(max_hits).encode("utf-8"),
                auth=self.auth,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                timeout=120,
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
            contact_name, contact_phone, contact_email = self._parse_contact_person(block)
            results.append({
                "internal_id": internal_id,
                "uuid": _first_tag(block, "UUID"),
                "name": _first_tag(block, "BusinessPartnerFormattedName") or _first_tag(block, "FirstLineName") or internal_id,
                "contact_person": contact_name,
                "email": _first_tag(block, "EMailURI") or contact_email,
                "phone": _first_tag(block, "CompleteNumberDescription") or _first_tag(block, "NormalisedNumberDescription") or contact_phone,
            })
        return results

