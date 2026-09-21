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
    if m:
        return m.group(1).strip()
    # Some operations on this service log validation errors under <Note>
    # instead of <Description> (confirmed live, Aug 2026, activate_site()).
    m = re.search(r"<(?:\w+:)?Note>(.*?)</(?:\w+:)?Note>", xml, re.S)
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

    def activate_site(self, material_id: str, site_id: str, company_id: str, procurement_type_code: str = "2",
                       valuation_data_client=None, product_category_id: str = None, set_of_books_id: str = None) -> dict:
        """Admin "Activate this site for this product" action (Aug 2026,
        user's explicit ask) - fixes the live "No valid planning data
        exists for product X in site Y" Stock Transfer failure by adding
        the missing site to an EXISTING material.

        `procurement_type_code` (Sep 5 2026 fix, real incident STO-135) -
        used to hardcode "2" ("External procurement") unconditionally,
        which SAP rejects for in-house-manufactured materials at
        manufacturing sites (needs "1") - see
        sap_material_client.get_existing_procurement_type_code, which the
        caller (server.py) now uses to derive the right value from the
        material's own existing sites before calling this.

        Two SEPARATE SOAP calls, not one bundle - confirmed live this
        service commits a bundle atomically (all-or-nothing), and
        Valuation has its own extra prerequisite (an Account Determination
        Group per company, Finance/Basis-owned) that Planning/Availability/
        Logistics don't need. Splitting them means a missing Account
        Determination Group only blocks Valuation, not the 3 fields that
        actually unblock the Stock Transfer check.

        BUG FIX (Sep 11 2026, real user report on 6800-004473): a site can
        already have a SupplyPlanning/AvailabilityConfirmation/Logistics
        NODE present while its own LifeCycleStatusCode is still "1" (In
        Preparation, SAP's yellow-warning state on the Logistics tab) -
        SAP rejects a plain actionCode="01" (create) against that with
        "already exists", which the OLD code treated as an unconditional
        success ("the site IS active") - false: the site was STILL stuck
        at "In Preparation", never actually flipped to "2" (Active). Live-
        confirmed the real fix: retry the exact same fields with
        actionCode="02" (update) on the sub-nodes when SAP says "already
        exists" - THIS actually flips LifeCycleStatusCode 1 -> 2 (verified
        live: 6800-004473 @ P2 went from "1" to a real "2" after this
        retry, matching SAP's own Logistics tab green check afterward).
        Only a genuine create-or-update SUCCESS is ever reported "ok" now.

        VALUATION FULL FIX (Sep 11 2026, user's exact business rules) -
        if the plain Valuation attempt fails with "Account det. group is
        missing" AND a `valuation_data_client` + `product_category_id` +
        `set_of_books_id` were given, calls
        SAPMaterialValuationDataClient.set_account_determination_and_price
        (see that module's docstring for the full live-derived schema)
        to set the Account Determination Group + Perpetual Cost Method
        (Moving Average) + an opening ValuationPrice of 0, THEN retries
        the Valuation update once more - live-confirmed this genuinely
        flips LifeCycleStatusCode 1 -> 2 for Valuation too (6800-004473 @
        both P2 and P4). Without those 3 extra args, falls back to the
        old behaviour (surfaces SAP's raw rejection reason).

        Returns {"planning_logistics": "ok"|<error str>, "valuation":
        "ok"|<error str>}."""
        def _planning_body(action_code: str) -> str:
            return f"""<n0:MaterialBundleMaintainRequest_sync_V1>
    <BasicMessageHeader><ID>{uuid.uuid4().hex.upper()}</ID></BasicMessageHeader>
    <Material actionCode="02">
        <InternalID>{material_id}</InternalID>
        <Planning>
            <SupplyPlanning actionCode="{action_code}">
                <SupplyPlanningAreaID>{site_id}</SupplyPlanningAreaID>
                <LifeCycleStatusCode>2</LifeCycleStatusCode>
                <ProcurementTypeCode>{procurement_type_code}</ProcurementTypeCode>
            </SupplyPlanning>
        </Planning>
        <AvailabilityConfirmation actionCode="{action_code}">
            <PlanningAreaID>{site_id}</PlanningAreaID>
            <LifeCycleStatusCode>2</LifeCycleStatusCode>
        </AvailabilityConfirmation>
        <Logistics actionCode="{action_code}">
            <SiteID>{site_id}</SiteID>
            <LifeCycleStatusCode>2</LifeCycleStatusCode>
        </Logistics>
    </Material>
</n0:MaterialBundleMaintainRequest_sync_V1>"""

        def _valuation_body(action_code: str) -> str:
            return f"""<n0:MaterialBundleMaintainRequest_sync_V1>
    <BasicMessageHeader><ID>{uuid.uuid4().hex.upper()}</ID></BasicMessageHeader>
    <Material actionCode="02">
        <InternalID>{material_id}</InternalID>
        <Valuation actionCode="{action_code}">
            <LifeCycleStatusCode>2</LifeCycleStatusCode>
            <CompanyID>{company_id}</CompanyID>
            <BusinessResidenceID>{site_id}</BusinessResidenceID>
        </Valuation>
    </Material>
</n0:MaterialBundleMaintainRequest_sync_V1>"""

        result = {}
        result["planning_logistics"] = self._create_then_update_on_conflict(_planning_body)

        valuation_result = self._create_then_update_on_conflict(_valuation_body)
        if "account det. group is missing" in valuation_result.lower() and valuation_data_client and product_category_id and set_of_books_id:
            try:
                valuation_data_client.set_account_determination_and_price(
                    material_id, company_id, site_id, product_category_id, set_of_books_id)
                valuation_result = self._create_then_update_on_conflict(_valuation_body)
            except Exception as e:
                # Sep 11 2026, real user report on 6700-302359 @ P9 - a site
                # that NEVER had ANY prior Valuation presence (unlike
                # 6800-004473's sites, which already had "In Preparation"
                # stub rows from some earlier point) hits a DIFFERENT, harder
                # wall here: "Valuation data missing for material X business
                # residence Y" - confirmed live this is NOT fixable via
                # either SOAP service (tried both ManageMaterialIn's plain
                # create AND ManageMaterialValuationDataIn's own actionCode
                # ="01" create - both refuse to bootstrap a level from
                # absolute zero). Per SAP's own docs, the very FIRST
                # valuation level for a site must be created once, manually,
                # via SAP UI (Inventory Valuation work center -> Material
                # valuation tab -> Maintain Product Specification Valuation)
                # - only AFTER that exists can either API adjust it further.
                if "valuation data missing" in str(e).lower():
                    valuation_result = (
                        f"{e} - this site has NEVER had a Valuation record for this material; ask your SAP admin "
                        f"to create it ONCE via Inventory Valuation work center -> Material valuation tab -> "
                        f"Maintain Product Specification Valuation, then Activate here again"
                    )
                else:
                    valuation_result = str(e)
        result["valuation"] = valuation_result
        return result

    def _create_then_update_on_conflict(self, body_fn) -> str:
        try:
            self._post(body_fn("01"))
            return "ok"
        except SAPMaterialCreateError as e:
            if "already exists" not in str(e).lower():
                return str(e)
        # Node already exists (possibly still "In Preparation") - retry
        # as an update so a real 1->2 status flip actually happens.
        try:
            self._post(body_fn("02"))
            return "ok"
        except SAPMaterialCreateError as e:
            return str(e)

    def set_valuation(self, material_id: str, site_id: str, company_id: str, amount: float,
                       valuation_data_client, product_category_id: str, set_of_books_id: str) -> dict:
        """"Set Valuation" admin tool (Sep 2026) - sets/updates a material's
        real Cost at one Company/Site, in ONE call, whether that site's
        Valuation is still "In Preparation" (activates it, per
        activate_site()'s docstring) OR already Active (adds a new Cost
        period - SAP's Moving Average price is period-based history, a new
        period row IS the update, no in-place edit exists). Always pushes
        the given `amount` first (unlike activate_site(), which only calls
        this as a 0-amount fallback on "account det. group is missing") so
        an already-Active row's Cost genuinely changes, not just a re-affirm
        of whatever price already existed. Returns {"status": "ok"|<error>}."""
        try:
            valuation_data_client.set_account_determination_and_price(
                material_id, company_id, site_id, product_category_id, set_of_books_id, amount=amount)
        except Exception as e:
            return {"status": str(e)}

        def _valuation_body(action_code: str) -> str:
            return f"""<n0:MaterialBundleMaintainRequest_sync_V1>
    <BasicMessageHeader><ID>{uuid.uuid4().hex.upper()}</ID></BasicMessageHeader>
    <Material actionCode="02">
        <InternalID>{material_id}</InternalID>
        <Valuation actionCode="{action_code}">
            <LifeCycleStatusCode>2</LifeCycleStatusCode>
            <CompanyID>{company_id}</CompanyID>
            <BusinessResidenceID>{site_id}</BusinessResidenceID>
        </Valuation>
    </Material>
</n0:MaterialBundleMaintainRequest_sync_V1>"""

        # No-op (returns "ok" via the "already exists" conflict path) if this
        # site's Valuation is already Active - only genuinely flips
        # LifeCycleStatusCode 1 -> 2 when it was still "In Preparation".
        return {"status": self._create_then_update_on_conflict(_valuation_body)}
