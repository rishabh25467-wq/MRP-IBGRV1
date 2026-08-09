"""Radish OMS (external Order Management System) client.

Provides the sales-forecast data used by the Purchasing Plan feature:
  - wm-part-map: OMS SKU/part number -> SAP part/BOM ID
  - sales-monthly/customers: list of customers with sales for a given month
  - sales-monthly/parts: per-customer part-level sales forecast for a month

Auth: POST /api/auth/login with username/password returns a Bearer JWT.
The token is cached and only re-fetched on expiry or a 401 response.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)

TOKEN_REFRESH_SECONDS = 30 * 60


class OMSError(Exception):
    pass


class OMSClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token = None
        self._token_fetched_at = 0

    def _login(self):
        try:
            resp = requests.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=20,
            )
        except requests.exceptions.RequestException as e:
            raise OMSError(f"Could not reach OMS: {e}")
        if resp.status_code != 200:
            raise OMSError(f"OMS login failed: HTTP {resp.status_code}")
        self._token = resp.json()["token"]
        self._token_fetched_at = time.time()

    def _get(self, path: str, params: dict = None):
        if not self._token or (time.time() - self._token_fetched_at) > TOKEN_REFRESH_SECONDS:
            self._login()
        try:
            resp = requests.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                params=params,
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise OMSError(f"Could not reach OMS at {path}: {e}")
        if resp.status_code == 401:
            self._login()
            try:
                resp = requests.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    params=params,
                    timeout=30,
                )
            except requests.exceptions.RequestException as e:
                raise OMSError(f"Could not reach OMS at {path}: {e}")
        if resp.status_code != 200:
            raise OMSError(f"OMS request to {path} failed: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_part_map(self) -> dict:
        """Returns {oms_part_no: sap_part_id}."""
        return self._get("/api/ms/wm-part-map").get("map", {})

    def get_customers(self, month: str) -> list:
        return self._get("/api/ms/sales-monthly/customers", {"month": month}).get("customers", [])

    def get_parts(self, month: str, customer: str, view: str = "fulfilment") -> list:
        """view='fulfilment' -> planned_qty/shipped_qty/open_qty fields (used
        by the Purchasing Plan's demand aggregation). view=None -> the OMS's
        DEFAULT "Sales" view fields (expected_qty/expected_inr/actual_qty/
        actual_inr) - this is what OMS's own Insights > Monthly Sales screen
        shows, and what get_sales_plan() uses for Sale Price/Sale Value so
        it reflects invoice pricing, not a fulfilment-specific number."""
        params = {"month": month, "customer": customer}
        if view:
            params["view"] = view
        return self._get("/api/ms/sales-monthly/parts", params).get("parts", [])

    def get_monthly_demand(self, month: str) -> dict:
        """Aggregates planned (forecast) quantity per OMS part number, across
        every customer, for a given 'YYYY-MM' month. Returns {part_no: qty}."""
        customers = self.get_customers(month)

        def fetch(customer_name):
            try:
                return self.get_parts(month, customer_name)
            except OMSError as e:
                logger.warning(f"Failed to fetch OMS parts for '{customer_name}' in {month}: {e}")
                return []

        names = [c["customer_name"] for c in customers if c.get("customer_name")]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(fetch, names))

        demand = {}
        for parts in results:
            for part in parts:
                part_no = part.get("part_no")
                qty = part.get("planned_qty") or 0
                if part_no:
                    demand[part_no] = demand.get(part_no, 0) + qty
        return demand

    def get_sales_plan(self, month: str) -> list:
        """Full sales plan for a 'YYYY-MM' month - the same underlying
        forecast get_monthly_demand() aggregates, but returned per part with
        a per-customer breakdown instead of collapsed into a single number.
        Also carries unit `price` (native currency, invoice-priced per the
        OMS's `price_basis: "invoice"` confirmation) and `sale_value_inr`,
        plus `lead_day` - the customer's requested/selling lead time in days
        for that part, used to back-calculate when procurement needs to
        start. All pulled from the OMS's DEFAULT "Sales" view (expected_qty/
        expected_inr - the same fields behind OMS's own Insights > Monthly
        Sales screen) rather than the fulfilment view.
        Backs the Purchasing Plan page's "Sales Plan Lookup" popup. Returns
        [{part_no, description, currency, price, lead_day, total_qty,
        total_sale_value_inr, customers: [{customer_name, qty, price,
        sale_value_inr, lead_day}]}] sorted by part_no, each part's
        customers sorted by qty descending."""
        customers = self.get_customers(month)

        def fetch(customer_name):
            try:
                return customer_name, self.get_parts(month, customer_name, view=None)
            except OMSError as e:
                logger.warning(f"Failed to fetch OMS parts for '{customer_name}' in {month}: {e}")
                return customer_name, []

        names = [c["customer_name"] for c in customers if c.get("customer_name")]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(fetch, names))

        by_part = {}
        for customer_name, parts in results:
            for part in parts:
                part_no = part.get("part_no")
                qty = part.get("expected_qty") or 0
                sale_value_inr = part.get("expected_inr") or 0
                if not part_no:
                    continue
                entry = by_part.setdefault(part_no, {
                    "part_no": part_no,
                    "description": part.get("name"),
                    "currency": part.get("currency"),
                    "price": part.get("price"),
                    "lead_day": part.get("lead_day"),
                    "total_qty": 0.0,
                    "total_sale_value_inr": 0.0,
                    "customers": [],
                })
                entry["total_qty"] += qty
                entry["total_sale_value_inr"] += sale_value_inr
                if qty:
                    entry["customers"].append({
                        "customer_name": customer_name, "qty": qty,
                        "price": part.get("price"), "sale_value_inr": sale_value_inr,
                        "lead_day": part.get("lead_day"),
                    })

        for entry in by_part.values():
            entry["customers"].sort(key=lambda c: -c["qty"])
        return sorted(by_part.values(), key=lambda e: e["part_no"])
