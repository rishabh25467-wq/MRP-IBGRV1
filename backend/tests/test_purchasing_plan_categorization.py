"""
Regression tests for iteration_17:
- Purchasing plan generation attaches a `category` field to each component
  (via bom_categorizer.categorize_items, best-effort).
- Uses target_month=2020-01 (fast path, ~18 components/5 categories per prior verification).
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

    deadline = time.time() + 120
    while time.time() < deadline:
        status_resp = requests.get(f"{BASE_URL}/api/purchasing-plan/status/{job_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        if data["status"] == "done":
            return data["result"]
        if data["status"] == "failed":
            pytest.fail(f"Job failed: {data.get('error')}")
        time.sleep(3)
    pytest.fail("Purchasing plan job did not complete within 120s")


class TestPurchasingPlanCategorization:
    def test_components_present(self, plan_result):
        assert len(plan_result["components"]) > 0

    def test_components_have_category_field(self, plan_result):
        components = plan_result["components"]
        categorized = [c for c in components if c.get("category")]
        # best-effort: at least most components should have a category
        assert len(categorized) >= len(components) * 0.5, (
            f"Expected majority of components categorized, got {len(categorized)}/{len(components)}"
        )

    def test_category_values_are_strings(self, plan_result):
        for c in plan_result["components"]:
            if c.get("category") is not None:
                assert isinstance(c["category"], str)
                assert len(c["category"]) > 0
