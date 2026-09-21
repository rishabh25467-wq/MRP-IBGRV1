"""SAP Business ByDesign ManageMaterialValuationDataIn SOAP client - sets
Account Determination Group + Perpetual Cost Method + an opening
ValuationPrice for an EXISTING ("In Preparation") material valuation
record, so a follow-up ManageMaterialIn Valuation actionCode="02" call
can then flip its LifeCycleStatusCode to "2" (Active). See
sap_material_create_client.activate_site()'s docstring for how the two
services chain together.

Sep 11 2026, user's explicit business rules + live-derived schema (no
official SAP XSD access from this tenant, everything below confirmed by
real trial calls against material 6800-004473, sites P2/P4):
- For "Business Residence" valuation level (type code "1" - the only
  level type this app ever uses, one row per Company+Site) you must
  OMIT ProductValuationLevelID entirely - only PermanentEstablishmentID
  + ProductValuationLevelTypeCode identify the row. Sending a
  ProductValuationLevelID here (even matching the site ID) makes SAP
  reject with "Valuation level X of type Business Residence does not
  exist" - confirmed live.
- ValuationPrice needs its own 4 fields (PriceTypeCode, ValidityDatePeriod
  StartDate/EndDate, SetOfBooksID, LocalCurrencyValuationPrice) - none
  optional, and StartDate MUST be the first calendar day of a month
  ("Cost of type Inventory Cost must be valid from first day of period"
  - confirmed live) or the WHOLE bundle is rejected atomically, meaning
  AccountDeterminationSpecification/InventoryValuationSpecification sent
  in the SAME call also silently fail to persist - confirmed live, had
  to resend all 3 together once ValuationPrice's fields were correct.
- User's exact business rules (Sep 11 2026): Account Determination
  Group by ProductCategoryID - RM=3010, SFG=3020, FG=3030, Scrap=Z001.
  Perpetual Cost Method: always "2" (Moving Average). PriceTypeCode:
  always "1" (Inventory Cost). Amount: always 0 at activation time.
  StartDate: today minus 40 days, rounded down to the 1st of that month.
  Chart of Accounts / SetOfBooksID: Company RI -> RSOB, RT -> RDOB (see
  sap_wip_clearing_client.company_and_set_of_books_for_site)."""
import re
import uuid
from datetime import date, timedelta

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

ACCOUNT_DETERMINATION_GROUP_BY_CATEGORY = {
    "RM": "3010",
    "SFG": "3020",
    "FG": "3030",
    "SCRAP": "Z001",
}


class SAPMaterialValuationDataError(Exception):
    pass


def _extract_note(xml: str):
    m = re.search(r"<(?:\w+:)?Note>(.*?)</(?:\w+:)?Note>", xml, re.S)
    return m.group(1).strip() if m else None


def valuation_price_start_date(today: date = None) -> str:
    """Always "current date - 40 days", rounded down to the 1st of that
    month - SAP requires an Inventory Cost's StartDate to be the first
    day of a (calendar-month) period."""
    d = (today or date.today()) - timedelta(days=40)
    return date(d.year, d.month, 1).isoformat()


class SAPMaterialValuationDataClient:
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
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                timeout=45,
            )
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            raise SAPMaterialValuationDataError(_extract_note(xml) or f"HTTP {resp.status_code}")
        # Sep 21 2026 fix (real live bug, first surfaced setting a genuine
        # non-zero Cost on an already-Active site - G12NUT @ P7): a <Log>
        # <Item> here is NOT always an error - SAP also logs an
        # INFORMATIONAL confirmation Note ("Inventory cost change document
        # ... created for company ...") on a real successful price change.
        # Only SeverityCode 3+ is an actual error - same convention already
        # used by sap_goods_movement_client.py/store_approval_service.py for
        # this exact SAP Log shape.
        if re.search(r"<SeverityCode>\s*[3-9]\s*</SeverityCode>", xml):
            raise SAPMaterialValuationDataError(_extract_note(xml) or "SAP rejected the valuation data")
        return xml

    def set_account_determination_and_price(self, material_id: str, company_id: str, site_id: str,
                                              product_category_id: str, set_of_books_id: str,
                                              amount: float = 0.0, start_date: str = None) -> None:
        """`amount`/`start_date` (Sep 2026, "Set Valuation" admin tool):
        lets a caller push a REAL Cost value instead of always bootstrapping
        at 0 - same ValuationPrice actionCode="01" (create a new price
        period), so this is also the correct call to update the Cost on a
        material/site whose Valuation is ALREADY Active (SAP's Moving
        Average price is period-based history, not an in-place edit - a new
        period row IS the update). AccountDeterminationSpecification/
        InventoryValuationSpecification still use actionCode="02" exactly
        as before regardless of whether they already exist (already
        confirmed live to work for both bootstrap and idempotent re-affirm)."""
        group_code = ACCOUNT_DETERMINATION_GROUP_BY_CATEGORY.get((product_category_id or "").strip().upper())
        if not group_code:
            raise SAPMaterialValuationDataError(
                f"No known Account Determination Group for product category '{product_category_id}' - ask your SAP admin for its code")
        start_date = start_date or valuation_price_start_date()
        body = f"""<n0:MaterialValuationDataBundleMaintainRequest_sync>
    <BasicMessageHeader><ID>{uuid.uuid4().hex.upper()}</ID></BasicMessageHeader>
    <MaterialValuationData actionCode="06">
        <MaterialInternalID>{material_id}</MaterialInternalID>
        <CompanyID>{company_id}</CompanyID>
        <AccountDeterminationSpecification actionCode="02">
            <PermanentEstablishmentID>{site_id}</PermanentEstablishmentID>
            <ProductValuationLevelTypeCode>1</ProductValuationLevelTypeCode>
            <AccountDeterminationMaterialValuationDataGroupCode>{group_code}</AccountDeterminationMaterialValuationDataGroupCode>
        </AccountDeterminationSpecification>
        <InventoryValuationSpecification actionCode="02">
            <PermanentEstablishmentID>{site_id}</PermanentEstablishmentID>
            <ProductValuationLevelTypeCode>1</ProductValuationLevelTypeCode>
            <PerpetualInventoryValuationProcedureCode>2</PerpetualInventoryValuationProcedureCode>
        </InventoryValuationSpecification>
        <ValuationPrice actionCode="01">
            <PermanentEstablishmentID>{site_id}</PermanentEstablishmentID>
            <ValidityDatePeriod>
                <StartDate>{start_date}</StartDate>
                <EndDate>9999-12-31</EndDate>
            </ValidityDatePeriod>
            <PriceTypeCode>1</PriceTypeCode>
            <SetOfBooksID>{set_of_books_id}</SetOfBooksID>
            <LocalCurrencyValuationPrice>
                <Amount currencyCode="INR">{amount}</Amount>
                <BaseQuantity unitCode="EA">1</BaseQuantity>
                <BaseQuantityTypeCode>EA</BaseQuantityTypeCode>
            </LocalCurrencyValuationPrice>
        </ValuationPrice>
    </MaterialValuationData>
</n0:MaterialValuationDataBundleMaintainRequest_sync>"""
        self._post(body)
