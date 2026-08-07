"""Tests for the Category Master feature (this session):
- POST /api/admin/categories (add_category) - new category creation, case-insensitive dedup
- GET /api/admin/components - categories list reflects newly added categories
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def api_client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


class TestCategoryMaster:
    def test_add_new_category_appears_in_list(self, api_client):
        new_name = f"TEST_Category_{uuid.uuid4().hex[:8]}"
        resp = api_client.post(f"{API}/admin/categories", json={"name": new_name})
        assert resp.status_code == 200
        data = resp.json()
        assert new_name in data["categories"]

        # verify it shows up via admin/components too
        get_resp = api_client.get(f"{API}/admin/components")
        assert get_resp.status_code == 200
        assert new_name in get_resp.json()["categories"]

    def test_add_duplicate_category_case_insensitive_is_noop(self, api_client):
        new_name = f"TEST_Dup_{uuid.uuid4().hex[:8]}"
        resp1 = api_client.post(f"{API}/admin/categories", json={"name": new_name})
        count1 = len(resp1.json()["categories"])

        resp2 = api_client.post(f"{API}/admin/categories", json={"name": new_name.upper()})
        assert resp2.status_code == 200
        count2 = len(resp2.json()["categories"])
        assert count1 == count2  # no duplicate added

    def test_add_category_empty_name_400(self, api_client):
        resp = api_client.post(f"{API}/admin/categories", json={"name": "   "})
        assert resp.status_code == 400

    def test_new_category_applies_to_component_via_patch(self, api_client):
        new_name = f"TEST_Applied_{uuid.uuid4().hex[:8]}"
        api_client.post(f"{API}/admin/categories", json={"name": new_name})
        # apply to a disposable TEST_ product id (avoid mutating shared seed data)
        test_product_id = f"TEST_PRODUCT_{uuid.uuid4().hex[:8]}"
        resp = api_client.patch(f"{API}/admin/components/{test_product_id}", json={"category": new_name})
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == new_name
        assert data["category_source"] == "manual"
