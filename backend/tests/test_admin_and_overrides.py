"""Tests for the new session's features:
- POST /api/purchasing-plan/part-overrides (missing-BOM manual override + immediate retry)
- GET/PATCH /api/admin/components + POST /api/admin/components/recategorize (Admin Component Master CRUD)
- AI categorization quality of pre-seeded items (RFID / spacers / manual-override protection)
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def api_client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


class TestPartOverrides:
    def test_valid_real_override_resolves(self, api_client):
        resp = api_client.post(f"{API}/purchasing-plan/part-overrides", json={
            "part_no": "E410", "sap_id": "E410_IN"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["part_no"] == "E410"
        assert data["sap_id"] == "E410_IN"
        assert data["resolved"] is True

    def test_bad_override_does_not_crash(self, api_client):
        resp = api_client.post(f"{API}/purchasing-plan/part-overrides", json={
            "part_no": "ZZZTEST", "sap_id": "DOES_NOT_EXIST_123"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved"] is False
        assert data["reason"]  # sensible non-empty reason
        assert isinstance(data["reason"], str)

    def test_retry_missing_uses_saved_override_automatically(self, api_client):
        # E410 override was saved above; retry-missing should now resolve it
        # WITHOUT the override being passed again.
        resp = api_client.post(f"{API}/purchasing-plan/retry-missing", json={
            "part_nos": ["E410"]
        })
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["part_no"] == "E410"
        assert results[0]["resolved"] is True
        assert results[0]["sap_id"] == "E410_IN"

    def test_missing_fields_400(self, api_client):
        resp = api_client.post(f"{API}/purchasing-plan/part-overrides", json={
            "part_no": "", "sap_id": ""
        })
        assert resp.status_code == 400


class TestAdminComponentsCRUD:
    def test_list_admin_components_shape(self, api_client):
        resp = api_client.get(f"{API}/admin/components")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data and "categories" in data
        assert isinstance(data["items"], list)
        assert isinstance(data["categories"], list)
        assert "Electronics" in data["categories"]
        assert "Plastic" in data["categories"]

    def test_patch_category_sets_manual_source(self, api_client):
        # Use a seeded test item that is safe to touch: CUCOIL-1.86
        resp = api_client.patch(f"{API}/admin/components/CUCOIL-1.86", json={"category": "Copper Alloy"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "Copper Alloy"
        assert data["category_source"] == "manual"

        # verify persisted
        get_resp = api_client.get(f"{API}/admin/components")
        items = {i["product_id"]: i for i in get_resp.json()["items"]}
        assert items["CUCOIL-1.86"]["category"] == "Copper Alloy"
        assert items["CUCOIL-1.86"]["category_source"] == "manual"

    def test_patch_msl_only_does_not_touch_category(self, api_client):
        before = api_client.get(f"{API}/admin/components").json()["items"]
        before_item = next(i for i in before if i["product_id"] == "SPC5WM")
        resp = api_client.patch(f"{API}/admin/components/SPC5WM", json={"msl": 500})
        assert resp.status_code == 200
        data = resp.json()
        assert data["msl"] == 500
        assert data["category"] == before_item["category"]  # unchanged
        assert data["category_source"] == before_item["category_source"]  # unchanged

    def test_recategorize_skips_manual(self, api_client):
        # SI-0038C-2 is manual per review request; CUCOIL-1.86 was just set manual above too.
        resp = api_client.post(f"{API}/admin/components/recategorize", json={
            "product_ids": ["SI-0038C-2", "CUCOIL-1.86"]
        })
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["skipped_manual"]) == {"SI-0038C-2", "CUCOIL-1.86"}
        assert data["categories"] == {}


class TestAICategorizationQuality:
    @pytest.fixture(scope="class")
    def components_by_id(self, api_client):
        resp = api_client.get(f"{API}/admin/components")
        return {i["product_id"]: i for i in resp.json()["items"]}

    def test_rfid_is_electronics(self, components_by_id):
        item = components_by_id.get("RFID-WM1942")
        assert item is not None, "RFID-WM1942 should be pre-seeded"
        assert item["category"] == "Electronics"

    def test_pp_spacer_is_plastic(self, components_by_id):
        item = components_by_id.get("SPC5WM")
        assert item is not None
        assert item["category"] == "Plastic"

    def test_nylon_spacer_is_plastic(self, components_by_id):
        item = components_by_id.get("SPC22WM")
        assert item is not None
        assert item["category"] == "Plastic"

    def test_manual_hex_bolt_stays_steel(self, components_by_id):
        item = components_by_id.get("SI-0038C-2")
        assert item is not None
        assert item["category"] == "Steel"
        assert item["category_source"] == "manual"
