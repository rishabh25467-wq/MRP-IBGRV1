"""Client for the Radish Technologies QMS Goods-Movement API (Aug 2026) -
a bespoke REST wrapper another team built over SAP ByDesign's inventory
SOAP services. NOT an API-key/OAuth integration: auth is a session cookie
obtained via POST /auth/login with an admin email/password, reused on
every subsequent call (~12h lifetime), refreshed once on a 401.

Wired into the Store Approval "issue stock" flow: when the store confirms
what was actually issued, this physically moves that quantity from the
picked source warehouse to the target bin in SAP. Radish returns HTTP 200
even when SAP itself reports a fault - callers MUST check the `ok` field,
not just the status code.
"""
import logging
from threading import Lock

import requests

logger = logging.getLogger(__name__)


class RadishQMSError(Exception):
    def __init__(self, status: int, message: str, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class RadishQMSClient:
    def __init__(self, base_url: str, email: str, password: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.lock = Lock()
        self.logged_in = False

    def _login(self) -> None:
        resp = self.session.post(
            f"{self.base_url}/auth/login",
            json={"email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RadishQMSError(resp.status_code, "Radish QMS login failed", resp.text[:500])
        self.logged_in = True

    def _post(self, path: str, payload: dict) -> dict:
        with self.lock:
            if not self.logged_in:
                self._login()
            resp = self.session.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
            if resp.status_code == 401:
                # session expired - log in exactly once more, then retry exactly once
                self.session.cookies.clear()
                self.logged_in = False
                self._login()
                resp = self.session.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        if resp.status_code == 403:
            raise RadishQMSError(403, "Radish QMS: authenticated user is not admin-scoped")
        if resp.status_code in (502, 503):
            raise RadishQMSError(resp.status_code, "Radish QMS/SAP unavailable", resp.text[:500])
        if resp.status_code >= 400:
            raise RadishQMSError(resp.status_code, f"Radish QMS request failed (HTTP {resp.status_code})", resp.text[:500])
        try:
            return resp.json()
        except ValueError:
            raise RadishQMSError(502, "Radish QMS returned a non-JSON response", resp.text[:500])

    def goods_movement(
        self, owner_party_id: str, product_id: str, source_logistics_area_id: str, target_logistics_area_id: str,
        quantity: float, quantity_uom: str, site_id: str, dry_run: bool = True,
        source_restricted: bool = False, target_restricted: bool = False,
    ) -> dict:
        """Wraps POST /byd/writeback/goods-movement. Returns Radish's raw
        response dict (has `ok`, `faults`, `external_id`, and on dry runs
        `envelope`) - always check `ok`, HTTP 200 does not mean success."""
        return self._post("/byd/writeback/goods-movement", {
            "owner_party_id": owner_party_id,
            "product_id": product_id,
            "source_logistics_area_id": source_logistics_area_id,
            "target_logistics_area_id": target_logistics_area_id,
            "quantity": quantity,
            "quantity_uom": quantity_uom,
            "source_restricted": source_restricted,
            "target_restricted": target_restricted,
            "site_id": site_id,
            "dry_run": dry_run,
        })
