"""Tests for MRP Plan Save/Autosave feature:
- GET /api/production-plan/mrp/autosave
- POST/GET /api/production-plan/mrp/saved-plans
- GET/DELETE /api/production-plan/mrp/saved-plans/{plan_id}
"""
import os
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')


@pytest.fixture(scope="module")
def api_client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="module")
def autosave_plan(api_client):
    """Fetch the existing autosave plan (main agent already generated one)."""
    resp = api_client.get(f"{BASE_URL}/api/production-plan/mrp/autosave")
    assert resp.status_code == 200
    data = resp.json()
    if not data.get("found"):
        pytest.skip("No autosave plan present yet - generate a plan first")
    return data


class TestAutosave:
    def test_autosave_found_and_structure(self, autosave_plan):
        assert autosave_plan["found"] is True
        assert "plan" in autosave_plan
        assert "created_at" in autosave_plan
        plan = autosave_plan["plan"]
        assert "components" in plan
        assert isinstance(plan["components"], list)
        assert len(plan["components"]) > 0
        comp = plan["components"][0]
        assert "product_id" in comp
        assert "demand_lines" in comp
        assert isinstance(comp["demand_lines"], list)

    def test_autosave_created_at_is_isoformat(self, autosave_plan):
        # Should be parseable
        from datetime import datetime
        datetime.fromisoformat(autosave_plan["created_at"])


class TestSavedPlansCRUD:
    def test_create_named_plan_TEST_(self, api_client, autosave_plan):
        payload = {
            "name": "TEST_Snapshot_Automated",
            "actor": "TEST_Agent",
            "plan": autosave_plan["plan"],
        }
        resp = api_client.post(f"{BASE_URL}/api/production-plan/mrp/saved-plans", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "TEST_Snapshot_Automated"
        assert data["created_by"] == "TEST_Agent"
        assert "id" in data
        assert isinstance(data["components_count"], int)
        assert data["components_count"] == len(autosave_plan["plan"]["components"])

    def test_full_crud_lifecycle(self, api_client, autosave_plan):
        """Single test to avoid shared state issues across xdist workers."""
        payload = {
            "name": "TEST_Snapshot_Lifecycle",
            "actor": "TEST_Agent",
            "plan": autosave_plan["plan"],
        }
        create_resp = api_client.post(f"{BASE_URL}/api/production-plan/mrp/saved-plans", json=payload)
        assert create_resp.status_code == 200
        plan_id = create_resp.json()["id"]

        # List should contain it, and never contain autosave
        list_resp = api_client.get(f"{BASE_URL}/api/production-plan/mrp/saved-plans")
        assert list_resp.status_code == 200
        list_data = list_resp.json()
        ids = [p["id"] for p in list_data["plans"]]
        assert plan_id in ids
        names = [p["name"] for p in list_data["plans"]]
        assert "Last Generated" not in names

        # Get by id matches
        get_resp = api_client.get(f"{BASE_URL}/api/production-plan/mrp/saved-plans/{plan_id}")
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["components"][0]["product_id"] == autosave_plan["plan"]["components"][0]["product_id"]

        # Delete
        del_resp = api_client.delete(f"{BASE_URL}/api/production-plan/mrp/saved-plans/{plan_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

        # Get after delete -> 404
        get_after_resp = api_client.get(f"{BASE_URL}/api/production-plan/mrp/saved-plans/{plan_id}")
        assert get_after_resp.status_code == 404

    def test_create_missing_name_returns_400(self, api_client, autosave_plan):
        payload = {"name": "  ", "actor": "TEST_Agent", "plan": autosave_plan["plan"]}
        resp = api_client.post(f"{BASE_URL}/api/production-plan/mrp/saved-plans", json=payload)
        assert resp.status_code == 400

    def test_create_missing_actor_returns_400(self, api_client, autosave_plan):
        payload = {"name": "TEST_Foo", "actor": "  ", "plan": autosave_plan["plan"]}
        resp = api_client.post(f"{BASE_URL}/api/production-plan/mrp/saved-plans", json=payload)
        assert resp.status_code == 400

    def test_delete_nonexistent_plan_returns_404(self, api_client):
        resp = api_client.delete(f"{BASE_URL}/api/production-plan/mrp/saved-plans/nonexistent-id-1234")
        assert resp.status_code == 404

    def test_cannot_delete_autosave_via_saved_plans_delete(self, api_client):
        """Autosave should be exempt from delete - not deletable via this endpoint."""
        resp = api_client.delete(f"{BASE_URL}/api/production-plan/mrp/saved-plans/autosave")
        assert resp.status_code == 404
