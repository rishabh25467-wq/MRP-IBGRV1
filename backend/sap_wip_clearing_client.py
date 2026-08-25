"""SAP Business ByDesign AccountingWIPClearingRun/CreateWip SOAP client -
triggers a real WIP Clearing Run for a single Production Lot so its
work-in-process inventory is zeroed out for period-end reporting, per
SAP's "Inventory Valuation - WIP Clearing" work center view.

Company/Set of Books and the fiscal calendar are tenant-specific business
config (not derivable from the WSDL) - confirmed directly by the user:
- Sites P1, P8, P5, P1W post under Company RI / Set of Books RSOB
- All other sites post under Company RT / Set of Books RDOB
- Fiscal year runs April 1 - March 31, labeled by its starting calendar
  year (e.g. August 2026 is FiscalYearID "2026", AccountingPeriodID "005")
- AccountingClosingStepCode is always "010" (Operational postings)
"""
import re
from datetime import date

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

SOAP_ACTION = "http://sap.com/xi/AP/FinancialAccounting/Global/AccountingWIPClearingRun/CreateWipRequest"

SITE_TO_COMPANY = {
    "P1": ("RI", "RSOB"),
    "P8": ("RI", "RSOB"),
    # Aug 27 2026, user's explicit ask: 2 more Company RI locations.
    "P5": ("RI", "RSOB"),
    # "P1W" is the ERP's own `comp.pcode` value for site W1 (confirmed
    # live, Aug 27 2026), not itself a site_id ever looked up here - kept
    # anyway (harmless) alongside the real site_id "W1", which the
    # live `comp` table also confirms is Company RI.
    "P1W": ("RI", "RSOB"),
    "W1": ("RI", "RSOB"),
}
DEFAULT_COMPANY = ("RT", "RDOB")


def company_and_set_of_books_for_site(site_id: str):
    return SITE_TO_COMPANY.get((site_id or "").strip().upper(), DEFAULT_COMPANY)


def current_fiscal_period_and_year(today: date = None):
    """Fiscal year Apr 1 - Mar 31, labeled by its starting calendar year.
    Period 1 = April ... Period 12 = March."""
    today = today or date.today()
    fiscal_year = today.year if today.month >= 4 else today.year - 1
    period = ((today.month - 4) % 12) + 1
    return f"{period:03d}", str(fiscal_year)


class SAPWipClearingError(Exception):
    def __init__(self, message: str, transaction_id: str = None):
        super().__init__(message)
        self.transaction_id = transaction_id


def _first_tag(xml: str, tag: str):
    m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


class SAPWipClearingClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def run_wip_clearing(self, production_lot_id: str, site_id: str, run_description: str = None) -> dict:
        """Submits an immediate (non-test) WIP Clearing Run for one
        Production Lot. Returns {"success": bool, "log": str|None}."""
        company_id, set_of_books_id = company_and_set_of_books_for_site(site_id)
        period_id, fiscal_year_id = current_fiscal_period_and_year()
        description = (run_description or f"Production Lot {production_lot_id} Confirmation")[:255]

        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:WIPCreateRequest xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <WIPRun>
        <ProductionLotID>{production_lot_id}</ProductionLotID>
        <RunDescription languageCode="EN">{description}</RunDescription>
        <AccountingPeriodID>{period_id}</AccountingPeriodID>
        <FiscalYearID>{fiscal_year_id}</FiscalYearID>
        <AccountingClosingStepCode>010</AccountingClosingStepCode>
        <CompanyID>{company_id}</CompanyID>
        <SetOfBooksID>{set_of_books_id}</SetOfBooksID>
        <BusinessResidence>{site_id}</BusinessResidence>
        <TestRunIndicator>false</TestRunIndicator>
      </WIPRun>
    </n0:WIPCreateRequest>
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
            raise SAPWipClearingError(f"Could not reach SAP: {e}")

        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            txn_match = re.search(r"Transaction ID ([A-F0-9]+)", faultstring)
            raise SAPWipClearingError(faultstring, transaction_id=txn_match.group(1) if txn_match else None)

        status_raw = _first_tag(xml, "WIPRunStatus")
        log_note = _first_tag(xml, "Log")
        return {"success": status_raw == "true", "log": log_note}
