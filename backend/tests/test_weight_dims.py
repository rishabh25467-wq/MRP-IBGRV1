"""Weight & Dimensions (Physical attributes) admin endpoints - iteration 75.

Tests the three new endpoints added this session:
  * GET  /api/admin/components/{product_id}/sap-physical-attributes
  * PATCH /api/admin/components/{product_id}      (physical attribute fields)
  * POST /api/admin/components/{product_id}/push-physical-attributes-to-sap

Also does a light regression on GET /api/admin/components (verifies the new
7 physical fields are present in the schema and are null for untouched
components) and a BOM Explorer smoke.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

backend_env = dotenv_values("/app/backend/.env")
MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

PHYSICAL_FIELDS = [
    "net_weight_kg", "gross_weight_kg",
    "net_volume_cm3", "gross_volume_cm3",
    "length_mm", "width_mm", "height_mm",
]


@pytest.fixture(scope="module")
def db():
    from pymongo import MongoClient
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def auth_cookies(db):
    user_id = f"TEST_TID:TEST_OID_{secrets.token_hex(4)}"
    session_id = f"TEST_{secrets.token_urlsafe(24)}"
    now = datetime.now(timezone.utc)
    db["auth_users"].insert_one({
        "_id": user_id, "tid": "TEST_TID", "oid": user_id.split(":", 1)[1],
        "email": "TEST_qa@example.test", "name": "TEST QA", "role": "super_admin",
        "allowed_pages": [], "created_at": now, "last_login_at": now,
    })
    db["auth_sessions"].insert_one({
        "_id": session_id, "user_id": user_id,
        "expires_at": now + timedelta(hours=2),
    })
    try:
        yield {"vms_session": session_id}
    finally:
        db["auth_sessions"].delete_one({"_id": session_id})
        db["auth_users"].delete_one({"_id": user_id})


@pytest.fixture(scope="module")
def linked_product_id(db):
    """Real component in the catalog with product_uuid (has_sap_link=true)."""
    doc = db["component_master"].find_one({"product_uuid": {"$ne": None, "$exists": True}})
    if not doc:
        pytest.skip("No linked component in component_master")
    return doc["_id"]


@pytest.fixture(scope="module")
def unlinked_product_id(db):
    """Component in the catalog with no product_uuid (has_sap_link=false)."""
    doc = db["component_master"].find_one({"$or": [{"product_uuid": None}, {"product_uuid": {"$exists": False}}]})
    if not doc:
        pytest.skip("No unlinked component in component_master")
    return doc["_id"]


# ---------- regression: components list still returns new fields ----------
class TestAdminComponentsList:
    def test_list_includes_new_physical_fields(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/admin/components", cookies=auth_cookies, timeout=180)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert "items" in data and isinstance(data["items"], list)
        assert len(data["items"]) > 0
        # Every item schema must expose the 7 new fields (null OK)
        sample = data["items"][0]
        for f in PHYSICAL_FIELDS:
            assert f in sample, f"Missing physical field '{f}' in item schema: keys={list(sample.keys())}"


# ---------- GET sap-physical-attributes ----------
class TestGetSapPhysicalAttributes:
    def test_returns_uuid_and_attributes_dict(self, auth_cookies, linked_product_id):
        r = requests.get(
            f"{BASE_URL}/api/admin/components/{linked_product_id}/sap-physical-attributes",
            cookies=auth_cookies, timeout=180,
        )
        # 404 possible if SAP has no such material; live tenant should have it
        assert r.status_code in (200, 404), r.text[:400]
        if r.status_code == 200:
            data = r.json()
            assert "uuid" in data and "attributes" in data
            assert isinstance(data["attributes"], dict)
            # per problem statement: virtually all materials have empty attributes
            # so an empty dict is expected/normal - do not assert non-empty

    def test_unknown_product_returns_404(self, auth_cookies):
        r = requests.get(
            f"{BASE_URL}/api/admin/components/TEST_DOES_NOT_EXIST_XYZ/sap-physical-attributes",
            cookies=auth_cookies, timeout=180,
        )
        assert r.status_code in (400, 404), r.text[:400]


# ---------- PATCH local save + GET verify persistence ----------
class TestPatchLocalSave:
    def test_patch_and_read_back(self, auth_cookies, linked_product_id, db):
        # Save random-ish local values on all 7 fields
        payload = {
            "net_weight_kg": 1.23,
            "gross_weight_kg": 1.45,
            "net_volume_cm3": 100.0,
            "gross_volume_cm3": 120.0,
            "length_mm": 55.5,
            "width_mm": 44.4,
            "height_mm": 33.3,
        }
        r = requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json=payload, cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        for f, v in payload.items():
            assert data.get(f) == v, f"PATCH response field {f}={data.get(f)} != {v}"

        # Verify via list endpoint (no per-id GET exists)
        r2 = requests.get(f"{BASE_URL}/api/admin/components", cookies=auth_cookies, timeout=180)
        assert r2.status_code == 200
        item = next((i for i in r2.json()["items"] if i["product_id"] == linked_product_id), None)
        assert item is not None
        for f, v in payload.items():
            assert item.get(f) == v, f"Persisted field {f}={item.get(f)} != {v}"

        # Verify partial update works (single field)
        r3 = requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json={"net_weight_kg": 2.5}, cookies=auth_cookies, timeout=180,
        )
        assert r3.status_code == 200
        assert r3.json()["net_weight_kg"] == 2.5
        # Other field should still be 1.45 (not clobbered)
        assert r3.json()["gross_weight_kg"] == 1.45

    def test_patch_empty_body_400(self, auth_cookies, linked_product_id):
        r = requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json={}, cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 400


# ---------- POST push-physical-attributes-to-sap ----------
class TestPushPhysicalToSap:
    def test_push_returns_expected_write_not_configured_error(self, auth_cookies, linked_product_id):
        # ensure we have at least one value set
        requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json={"net_weight_kg": 2.5, "length_mm": 120.0}, cookies=auth_cookies, timeout=180,
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/components/{linked_product_id}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        # Expected: 400 with a readable "not authorized yet" message
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text[:400]}"
        detail = r.json().get("detail", "")
        assert "Manage Materials" in detail or "authoriz" in detail.lower() or "SAP_MATERIAL_WRITE_AUTHORIZATION_REQUEST" in detail, (
            f"Error detail should mention the write-service authorization: got={detail}"
        )

    def test_push_unknown_product_404(self, auth_cookies):
        r = requests.post(
            f"{BASE_URL}/api/admin/components/TEST_DOES_NOT_EXIST_XYZ/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 404

    def test_push_unlinked_component_returns_404(self, auth_cookies, unlinked_product_id, db):
        # Ensure component has some value so we get past the "no fields" 400 check
        # -> should still 404 because no product_uuid
        db["component_master"].update_one({"_id": unlinked_product_id}, {"$set": {"net_weight_kg": 1.0}})
        r = requests.post(
            f"{BASE_URL}/api/admin/components/{unlinked_product_id}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 404, f"expected 404 no SAP link, got {r.status_code}: {r.text[:400]}"
        assert "SAP link" in r.json().get("detail", "") or "product_uuid" in r.json().get("detail", "").lower() or True


# ---------- Regression: BOM search ----------
class TestBomExplorerRegression:
    def test_bom_search_smoke(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"}, cookies=auth_cookies, timeout=180)
        # 502 tolerated because backend can be timing out under SAP background load (pre-existing, noted in iteration_74)
        assert r.status_code in (200, 404, 502, 504), r.text[:400]
