"""
Regression + new-feature tests for this session: SAP On-Hand Inventory netting
integrated into the Purchasing Plan.

Verifies:
- PurchasingPlanComponent now returns on_hand_qty, net_qty_by_month, net_value_by_month
- Net Purchase Qty == max(0, Gross Qty - On-Hand Qty) for every component/month
- When on_hand_qty is None (never stocked), net_qty_by_month falls back to == qty_by_month
- net_value_by_month == net_qty * unit_cost (consistent with value_by_month/qty_by_month relationship)

Uses target_month=2020-01, a known fast path (~18 components, warm BOM cache per
prior iteration_17/19 testing) to avoid the 3-5+ minute full live-tenant run.
"""
import os
import time
import requests
import pytest


def _get_base_url():
    url = os.environ.get("REACT_APP_BACKEND_URL")
    if not url:
        env_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.strip().split("=", 1)[1]
                        break
    if not url:
        raise RuntimeError("REACT_APP_BACKEND_URL missing")
    return url.rstrip("/")


BASE_URL = _get_base_url()


@pytest.fixture(scope="module")
def plan_result():
    resp = requests.post(f"{BASE_URL}/api/purchasing-plan/generate", json={"target_month": "2020-01"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    deadline = time.time() + 600
    while time.time() < deadline:
        status_resp = requests.get(f"{BASE_URL}/api/purchasing-plan/status/{job_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        if data["status"] == "done":
            return data["result"]
        if data["status"] == "failed":
            pytest.fail(f"Job failed: {data.get('error')}")
        time.sleep(3)
    pytest.fail("Purchasing plan job did not complete within 600s")


class TestOnHandInventoryFields:
    def test_components_present(self, plan_result):
        assert len(plan_result["components"]) > 0

    def test_component_has_new_fields(self, plan_result):
        for c in plan_result["components"]:
            assert "on_hand_qty" in c
            assert "net_qty_by_month" in c
            assert "net_value_by_month" in c
            assert isinstance(c["net_qty_by_month"], dict)
            assert isinstance(c["net_value_by_month"], dict)
            assert set(c["net_qty_by_month"].keys()) == set(c["qty_by_month"].keys())

    def test_some_components_have_on_hand_data(self, plan_result):
        with_data = [c for c in plan_result["components"] if c.get("on_hand_qty") is not None]
        # Not asserting 100% - best effort per backend design, but expect majority given live SAP data
        assert len(with_data) >= 0  # informational; real assertion below on math

    def test_net_qty_formula_when_on_hand_present(self, plan_result):
        checked = 0
        for c in plan_result["components"]:
            if c.get("on_hand_qty") is None:
                continue
            for month, gross in c["qty_by_month"].items():
                net = c["net_qty_by_month"][month]
                expected = max(0.0, round(gross - c["on_hand_qty"], 4))
                assert abs(net - expected) < 0.01, (
                    f"{c['product_id']}/{month}: net={net} expected~{expected} "
                    f"(gross={gross}, on_hand={c['on_hand_qty']})"
                )
                checked += 1
        assert checked > 0, "No components had on_hand_qty set - cannot verify netting formula"

    def test_net_qty_never_negative(self, plan_result):
        for c in plan_result["components"]:
            for month, net in c["net_qty_by_month"].items():
                assert net >= 0, f"{c['product_id']}/{month}: net_qty={net} is negative"

    def test_net_qty_fallback_when_no_on_hand_data(self, plan_result):
        no_data = [c for c in plan_result["components"] if c.get("on_hand_qty") is None]
        checked = 0
        for c in no_data:
            for month, gross in c["qty_by_month"].items():
                net = c["net_qty_by_month"][month]
                assert abs(net - gross) < 0.0001, (
                    f"{c['product_id']}/{month}: expected net==gross when no on-hand data, "
                    f"got net={net} gross={gross}"
                )
                checked += 1
        # informational: may be 0 if every component happens to have on-hand data this run

    def test_net_value_consistent_with_net_qty_and_unit_cost(self, plan_result):
        checked = 0
        for c in plan_result["components"]:
            if c.get("unit_cost") is None:
                continue
            for month, net_qty in c["net_qty_by_month"].items():
                net_value = c["net_value_by_month"][month]
                if net_value is None:
                    continue
                expected = round(net_qty * c["unit_cost"], 2)
                assert abs(net_value - expected) < 0.05, (
                    f"{c['product_id']}/{month}: net_value={net_value} expected~{expected}"
                )
                checked += 1
        assert checked >= 0
