"""SAP Business ByDesign ManageMaterialIn SOAP client - creates a new
Material master record via MaintainBundle_V1 (actionCode="01").

Live-confirmed 20 Aug 2026: creation works correctly on the first try
(Material BK-... style records get a real UUID assigned, LifeCycleStatus
defaults to "In Preparation" since no Purchasing/Sales/Logistics/Valuation
sub-nodes are sent). The documented CheckMaintainBundle_V1 "dry run"
operation does NOT actually dry-run on this tenant's Communication
Arrangement - sending a different SOAPAction header still executed the
real create - so this client ONLY ever performs the real create; there is
no safe simulate-first path here. A delete (actionCode="03") on the same
InternalID DOES work for cleaning up an unused/never-activated ("In
Preparation") material - confirmed by creating then deleting a real test
material (ZTEST-CHECK-001) and verifying 0 query hits afterward."""
import uuid

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageMaterialIn/MaintainBundle_V1Request"


class SAPMaterialCreateError(Exception):
    pass


def _extract_fault(xml: str) -> str:
    import re
    m = re.search(r"<(?:\w+:)?faultstring>(.*?)</(?:\w+:)?faultstring>", xml, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"<(?:\w+:)?Description>(.*?)</(?:\w+:)?Description>", xml, re.S)
    return m.group(1).strip() if m else None


class SAPMaterialCreateClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def _post(self, body_xml: str) -> str:
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/>
 <soapenv:Body>{body_xml}</soapenv:Body>
</soapenv:Envelope>"""
        with sap_semaphore:
            resp = requests.post(
                self.endpoint, data=envelope.encode("utf-8"), auth=self.auth,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": f'"{SOAP_ACTION}"'},
                timeout=45,
            )
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            raise SAPMaterialCreateError(_extract_fault(xml) or f"HTTP {resp.status_code}")
        log_error = _extract_fault(xml) if "<Log>" in xml and "Item" in xml else None
        if log_error:
            raise SAPMaterialCreateError(log_error)
        return xml

    def create_material(self, material_id: str, product_category_id: str, base_uom: str, description: str) -> dict:
        """Creates a brand-new Material master record. Raises
        SAPMaterialCreateError on any SAP-side rejection (e.g. invalid
        ProductCategoryID, or the ID already exists). Returns
        {"material_id": str, "uuid": str}."""
        body = f"""<n0:MaterialBundleMaintainRequest_sync_V1>
    <BasicMessageHeader><ID>{uuid.uuid4().hex.upper()}</ID></BasicMessageHeader>
    <Material actionCode="01">
        <InternalID>{material_id}</InternalID>
        <ProductCategoryID>{product_category_id}</ProductCategoryID>
        <BaseMeasureUnitCode>{base_uom}</BaseMeasureUnitCode>
        <Description actionCode="01">
            <Description languageCode="EN">{description}</Description>
        </Description>
    </Material>
</n0:MaterialBundleMaintainRequest_sync_V1>"""
        xml = self._post(body)
        import re
        uuid_match = re.search(r"<(?:\w+:)?UUID>(.*?)</(?:\w+:)?UUID>", xml)
        returned_id = re.search(r"<(?:\w+:)?InternalID>(.*?)</(?:\w+:)?InternalID>", xml)
        if not uuid_match or not returned_id:
            raise SAPMaterialCreateError("SAP did not confirm the material was created (no UUID in response)")
        return {"material_id": returned_id.group(1).strip(), "uuid": uuid_match.group(1).strip()}

    def delete_material(self, material_id: str) -> None:
        """Deletes an unused ('In Preparation', never activated) Material.
        Only intended for cleaning up mistakes - SAP will reject this if
        the material has since been referenced anywhere."""
        body = f"""<n0:MaterialBundleMaintainRequest_sync_V1>
    <BasicMessageHeader><ID>{uuid.uuid4().hex.upper()}</ID></BasicMessageHeader>
    <Material actionCode="03">
        <InternalID>{material_id}</InternalID>
    </Material>
</n0:MaterialBundleMaintainRequest_sync_V1>"""
        self._post(body)
