"""Client for SAP Business ByDesign's generic Query Object Descriptions
service (QueryObjectDescriptionIn, operation FindObjectDescriptionByElements)
- used here ONLY for Object Type Code 176 (Fixed Asset), which returns
every Fixed Asset master record (Master Fixed Asset ID + Description) the
tenant has, so the Capital PO form can show a real dropdown instead of us
ever creating a new asset - per the user's explicit ask, Sep 15 2026.

Confirmed live against this tenant: the response's ObjectContextID is
"{CompanyID} {MasterFixedAssetID}" (single space, per SAP's own docs on
Fixed Asset's context = Company ID + Master Fixed Asset ID), e.g.
"RI 45" -> company RI, asset 45. ObjectID itself is always "0" for this
object type on this tenant - NOT the real key, ignore it.

Sep 15 2026, user's ask - "if PR from P9 only P9 fixed asset list needed":
this service has NO Site field at all (context is only Company + Master
Fixed Asset ID), but every single one of this tenant's 125 Fixed Asset
descriptions is manually prefixed with its Site (e.g. "P9-Machine",
"P7-Batteries", loosely formatted - some have a space before/after the
hyphen). That prefix is the only signal available, so it's parsed out
here as `site_id` for per-site filtering.

Authorization: requires the "Query Object Descriptions" service role
(operation "Find object descriptions") on the `_EMERGENTBOM` technical
user - granted Sep 15 2026 via Communication Arrangement
"fixed_asset_Emergent". SAP_SOAP_OBJECT_DESCRIPTION_ENDPOINT in backend/.env.
"""
import re
import xml.etree.ElementTree as ET

import requests

from sap_rate_limiter import sap_semaphore

FIXED_ASSET_OBJECT_TYPE_CODE = "176"
SOAP_ACTION = "http://sap.com/xi/A1S/Global/QueryObjectDescriptionIn/FindObjectDescriptionByElementsRequest"
_SITE_PREFIX_RE = re.compile(r"^\s*(P\d+W?)\s*-\s*", re.IGNORECASE)

_REQUEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:ObjectDescriptionByElementsQuery_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
    <ObjectDescriptionSelectionByElements>
        <SelectionByObjectTypeCode>{object_type_code}</SelectionByObjectTypeCode>
    </ObjectDescriptionSelectionByElements>
    <ProcessingConditions>
        <QueryHitsMaximumNumberValue>{limit}</QueryHitsMaximumNumberValue>
        <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
    </ProcessingConditions>
</n0:ObjectDescriptionByElementsQuery_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPFixedAssetError(Exception):
    pass


class SAPFixedAssetClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.username = username
        self.password = password

    def list_fixed_assets(self, limit: int = 1000) -> list:
        """Returns every Fixed Asset in the tenant:
        [{"company_code", "asset_id", "description", "site_id"}, ...].
        site_id is None if the description doesn't carry a recognizable
        "P<n>-" prefix (a handful of older records don't)."""
        if not self.endpoint:
            raise SAPFixedAssetError("Fixed Asset lookup isn't wired up yet - SAP_SOAP_OBJECT_DESCRIPTION_ENDPOINT is not set.")
        body = _REQUEST_TEMPLATE.format(object_type_code=FIXED_ASSET_OBJECT_TYPE_CODE, limit=limit)
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    auth=(self.username, self.password),
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
                    timeout=60,
                )
        except requests.exceptions.RequestException as e:
            raise SAPFixedAssetError(f"Could not reach SAP: {e}")

        if resp.status_code != 200 or "Fault" in resp.text[:2000]:
            raise SAPFixedAssetError(f"SAP Fixed Asset query failed (HTTP {resp.status_code}): {resp.text[:400]}")

        root = ET.fromstring(resp.text)
        rows = []
        for od in root.findall(".//ObjectDescription"):
            context_id = (od.findtext("ObjectContextID") or "").strip()
            if " " not in context_id:
                continue
            company_code, asset_id = context_id.split(" ", 1)
            description = od.findtext("Description") or asset_id.strip()
            site_match = _SITE_PREFIX_RE.match(description)
            rows.append({
                "company_code": company_code.strip(),
                "asset_id": asset_id.strip(),
                "description": description,
                "site_id": site_match.group(1).upper() if site_match else None,
            })
        rows.sort(key=lambda r: r["description"].lower())
        return rows
