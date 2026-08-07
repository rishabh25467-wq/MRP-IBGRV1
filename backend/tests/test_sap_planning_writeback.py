"""Tests for the SAP MSL Write-Back feature added this session:
- sap_planning_client.py unit-level helpers (_duration_to_days / _days_to_duration)
- GET /api/admin/components/{product_id}/sap-planning
- POST /api/admin/components/{product_id}/push-to-sap
- PATCH /api/admin/components/{product_id} now also accepting lead_time_days

Primary live test subject: SPC5WM (confirmed has_sap_link=true, real SAP tenant).
"""
import os
import sys

import pytest
import requests
from dotenv import dotenv_values

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sap_planning_client import _duration_to_days, _days_to_duration  # noqa: E402

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"
SAP_LINKED_COMPONENT = "SPC5WM"


@pytest.fixture
def api_client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


class TestDurationHelpers:
    """Unit-level checks on sap_planning_client.py duration conversion helpers."""

    def test_duration_to_days_valid(self):
        assert _duration_to_days("P4D") == 4.0

    def test_duration_to_days_empty(self):
        assert _duration_to_days("") is None

    def test_duration_to_days_none(self):
        assert _duration_to_days(None) is None

    def test_duration_to_days_invalid_format(self):
        assert _duration_to_days("garbage") is None

    def test_days_to_duration(self):
        assert _days_to_duration(7) == "P7D"

    def test_days_to_duration_float(self):
        assert _days_to_duration(4.0) == "P4D"


class TestAdminComponentsListing:
    def test_list_admin_components_shape(self, api_client):
        resp = api_client.get(f"{API}/admin/components")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data and "categories" in data
        assert isinstance(data["items"], list)
        assert len(data["items"]) > 0
        sample = data["items"][0]
        for field in ("product_id", "has_sap_link", "sap_pushed_at", "lead_time_days"):
            assert field in sample

    def test_spc5wm_has_sap_link(self, api_client):
        resp = api_client.get(f"{API}/admin/components")
        items = resp.json()["items"]
        spc = next((i for i in items if i["product_id"] == SAP_LINKED_COMPONENT), None)
        assert spc is not None, "SPC5WM should exist in component_master"
        assert spc["has_sap_link"] is True


class TestLeadTimePatch:
    def test_patch_lead_time_persists(self, api_client):
        resp = api_client.patch(f"{API}/admin/components/{SAP_LINKED_COMPONENT}", json={"lead_time_days": 7})
        assert resp.status_code == 200
        data = resp.json()
        assert data["lead_time_days"] == 7
        assert data["product_id"] == SAP_LINKED_COMPONENT

        # verify persistence via GET
        get_resp = api_client.get(f"{API}/admin/components")
        items = get_resp.json()["items"]
        spc = next(i for i in items if i["product_id"] == SAP_LINKED_COMPONENT)
        assert spc["lead_time_days"] == 7


class TestSapPlanningPull:
    def test_get_sap_planning_for_linked_component(self, api_client):
        resp = api_client.get(f"{API}/admin/components/{SAP_LINKED_COMPONENT}/sap-planning")
        assert resp.status_code == 200
        data = resp.json()
        assert "safety_stock" in data
        assert "lead_time_days" in data
        assert "planning_area_count" in data
        assert data["planning_area_count"] >= 1

    def test_get_sap_planning_for_unknown_component_404(self, api_client):
        resp = api_client.get(f"{API}/admin/components/TEST_NONEXISTENT_PRODUCT_ID_999/sap-planning")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data


class TestPushToSap:
    def test_push_to_sap_updates_all_planning_areas(self, api_client):
        # Ensure msl/lead_time_days set first
        api_client.patch(f"{API}/admin/components/{SAP_LINKED_COMPONENT}", json={"msl": 500, "lead_time_days": 7})

        resp = api_client.post(f"{API}/admin/components/{SAP_LINKED_COMPONENT}/push-to-sap")
        assert resp.status_code == 200
        data = resp.json()
        assert data["planning_areas_updated"] >= 1
        assert data["safety_stock"] == 500
        assert data["lead_time_days"] == 7

        # verify sap_pushed_at timestamp updated
        get_resp = api_client.get(f"{API}/admin/components")
        items = get_resp.json()["items"]
        spc = next(i for i in items if i["product_id"] == SAP_LINKED_COMPONENT)
        assert spc["sap_pushed_at"] is not None

    def test_push_to_sap_unknown_component_404(self, api_client):
        resp = api_client.post(f"{API}/admin/components/TEST_NONEXISTENT_PRODUCT_ID_999/push-to-sap")
        assert resp.status_code == 404
