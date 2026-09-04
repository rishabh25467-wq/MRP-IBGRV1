"""Client for the external SCM.AI "Smart Approvals" PO Integration API -
reads an already-approved Purchase Requisition (PR) by its voucher number
and, once we create the matching SAP PO, stamps the SAP PO number back onto
that PR so both systems stay in sync.

Sep 4 2026, user's explicit ask: PR Number on the Purchase Order Creation
page must MANDATORILY fetch and auto-fill vendor/site/line items from this
PR (not a free-text reference field like before).

Contract: https://smart-approve.preview.emergentagent.com/api/public/docs/po-integration.md
Auth: static `X-Api-Key` header (scope `po-integration`, revocable)."""
import requests


class PRIntegrationError(Exception):
    pass


class PRIntegrationClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def list_approved(self, limit: int = 50, offset: int = 0, po_status: str = None, supplier_pcode: str = None) -> dict:
        params = {"limit": limit, "offset": offset}
        if po_status:
            params["po_status"] = po_status
        if supplier_pcode:
            params["supplier_pcode"] = supplier_pcode
        try:
            resp = requests.get(
                f"{self.base_url}/api/po-integration/approved",
                params=params, headers=self._headers(), timeout=(5, 15),
            )
        except requests.exceptions.RequestException as e:
            raise PRIntegrationError(f"Could not reach the PR system: {e}")
        if resp.status_code != 200:
            raise PRIntegrationError(f"PR list failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def get_pr_detail(self, voc_no: str) -> dict:
        try:
            resp = requests.get(
                f"{self.base_url}/api/po-integration/{voc_no}",
                headers=self._headers(), timeout=(5, 15),
            )
        except requests.exceptions.RequestException as e:
            raise PRIntegrationError(f"Could not reach the PR system: {e}")
        if resp.status_code == 404:
            raise PRIntegrationError(f"PR {voc_no} was not found")
        if resp.status_code == 409:
            detail = "PR is not fully approved yet"
            try:
                detail = resp.json().get("detail", detail)
            except ValueError:
                pass
            raise PRIntegrationError(detail)
        if resp.status_code != 200:
            raise PRIntegrationError(f"PR lookup failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def stamp_po_created(self, voc_no: str, po_number: str, po_date: str, po_amount: float = None, notes: str = None) -> dict:
        payload = {"po_number": po_number, "po_date": po_date}
        if po_amount is not None:
            payload["po_amount"] = po_amount
        if notes:
            payload["notes"] = notes
        try:
            resp = requests.post(
                f"{self.base_url}/api/po-integration/{voc_no}/po-created",
                json=payload, headers=self._headers(), timeout=(5, 15),
            )
        except requests.exceptions.RequestException as e:
            raise PRIntegrationError(f"Could not reach the PR system to stamp the PO back: {e}")
        if resp.status_code not in (200, 201):
            raise PRIntegrationError(f"PO stamp-back failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()
