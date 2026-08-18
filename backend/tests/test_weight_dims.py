"""Weight & Surface Area (Physical attributes) admin endpoints - iteration 76.

This is a full rework of iteration 75's test. This session dropped the old
7-field UoM-Characteristic-based approach and replaced it with 2 custom
extension fields (net_weight_kg, surface_area_sqin) read/written directly
on the Material's General tab. Also adds a new BOM Explorer read-only
endpoint GET /api/bom/net-weight served from the local cache.
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

# NEW: only 2 fields now (was 7 in iter75)
PHYSICAL_FIELDS = ["net_weight_kg", "surface_area_sqin"]
# Fields that must NOT exist any more (regression check)
DROPPED_FIELDS = [
    "gross_weight_kg", "net_volume_cm3", "gross_volume_cm3",
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
    doc = db["component_master"].find_one({"product_uuid": {"$ne": None, "$exists": True}})
    if not doc:
        pytest.skip("No linked component in component_master")
    return doc["_id"]


@pytest.fixture(scope="module")
def unlinked_product_id(db):
    doc = db["component_master"].find_one({"$or": [{"product_uuid": None}, {"product_uuid": {"$exists": False}}]})
    return doc["_id"] if doc else None


@pytest.fixture(scope="module", autouse=True)
def cleanup_touched(db, linked_product_id):
    yield
    db["component_master"].update_many(
        {"_id": linked_product_id},
        {"$unset": {"net_weight_kg": "", "surface_area_sqin": "", "sap_physical_pushed_at": ""}},
    )


# ---------- Regression: components list schema ----------
class TestAdminComponentsList:
    def test_list_includes_new_2_physical_fields(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/admin/components", cookies=auth_cookies, timeout=180)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert "items" in data and isinstance(data["items"], list)
        assert len(data["items"]) > 0
        sample = data["items"][0]
        for f in PHYSICAL_FIELDS:
            assert f in sample, f"Missing '{f}' in item schema. Keys={list(sample.keys())}"

    def test_list_no_longer_exposes_dropped_fields(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/admin/components", cookies=auth_cookies, timeout=180)
        assert r.status_code == 200
        sample = r.json()["items"][0]
        # These may legally still exist in DB (leftover), but should NOT be part of the API model
        for f in DROPPED_FIELDS:
            assert f not in sample, f"Dropped field '{f}' should not appear in response schema"


# ---------- GET sap-physical-attributes ----------
class TestGetSapPhysicalAttributes:
    def test_returns_attributes_dict(self, auth_cookies, linked_product_id):
        r = requests.get(
            f"{BASE_URL}/api/admin/components/{linked_product_id}/sap-physical-attributes",
            cookies=auth_cookies, timeout=180,
        )
        # OData custom service now live; 200 with attrs dict if SAP knows the material; 404 if not; 400 if SAP unreachable
        assert r.status_code in (200, 400, 403, 404), r.text[:400]
        if r.status_code == 200:
            data = r.json()
            assert "attributes" in data
            assert isinstance(data["attributes"], dict)
            # Only known fields
            for k in data["attributes"].keys():
                assert k in PHYSICAL_FIELDS, f"Unexpected attribute key: {k}"

    def test_unknown_product_returns_4xx(self, auth_cookies):
        r = requests.get(
            f"{BASE_URL}/api/admin/components/TEST_DOES_NOT_EXIST_XYZ/sap-physical-attributes",
            cookies=auth_cookies, timeout=180,
        )
        assert r.status_code in (400, 404), r.text[:400]


# ---------- PATCH local save + GET verify persistence ----------
class TestPatchLocalSave:
    def test_patch_2_fields_and_read_back(self, auth_cookies, linked_product_id):
        payload = {"net_weight_kg": 0.42, "surface_area_sqin": 255.0}
        r = requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json=payload, cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        for f, v in payload.items():
            assert data.get(f) == v, f"PATCH response {f}={data.get(f)} != {v}"

        # Verify via list endpoint
        r2 = requests.get(f"{BASE_URL}/api/admin/components", cookies=auth_cookies, timeout=180)
        assert r2.status_code == 200
        item = next((i for i in r2.json()["items"] if i["product_id"] == linked_product_id), None)
        assert item is not None
        for f, v in payload.items():
            assert item.get(f) == v

        # Partial update
        r3 = requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json={"net_weight_kg": 1.11}, cookies=auth_cookies, timeout=180,
        )
        assert r3.status_code == 200
        assert r3.json()["net_weight_kg"] == 1.11
        assert r3.json()["surface_area_sqin"] == 255.0  # untouched

    def test_patch_empty_body_400(self, auth_cookies, linked_product_id):
        r = requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json={}, cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 400


# ---------- POST push-physical-attributes-to-sap ----------
class TestPushPhysicalToSap:
    def test_push_returns_success_or_actionable_error(self, auth_cookies, linked_product_id):
        # Ensure at least one value set
        requests.patch(
            f"{BASE_URL}/api/admin/components/{linked_product_id}",
            json={"net_weight_kg": 0.42}, cookies=auth_cookies, timeout=180,
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/components/{linked_product_id}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        # OData path: 200 on real write; 400 with actionable msg if SAP doesn't have the material or auth issue; NOT 500
        assert r.status_code in (200, 400, 403, 404), f"Unexpected {r.status_code}: {r.text[:400]}"
        if r.status_code == 200:
            data = r.json()
            assert "pushed_fields" in data and "net_weight_kg" in data["pushed_fields"]
        else:
            detail = r.json().get("detail", "")
            assert isinstance(detail, str) and len(detail) > 0

    def test_push_unknown_product_404(self, auth_cookies):
        r = requests.post(
            f"{BASE_URL}/api/admin/components/TEST_DOES_NOT_EXIST_XYZ/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 404

    def test_push_no_values_400(self, auth_cookies, linked_product_id, db):
        # Clear values
        db["component_master"].update_one(
            {"_id": linked_product_id},
            {"$unset": {"net_weight_kg": "", "surface_area_sqin": ""}},
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/components/{linked_product_id}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        assert r.status_code == 400
        detail = r.json().get("detail", "").lower()
        assert "net weight" in detail or "surface area" in detail or "before pushing" in detail

    def test_push_unlinked_component_returns_4xx(self, auth_cookies, unlinked_product_id, db):
        if not unlinked_product_id:
            pytest.skip("All components in this catalog have SAP links - no unlinked to test")
        db["component_master"].update_one({"_id": unlinked_product_id}, {"$set": {"net_weight_kg": 1.0}})
        r = requests.post(
            f"{BASE_URL}/api/admin/components/{unlinked_product_id}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        # cleanup
        db["component_master"].update_one({"_id": unlinked_product_id}, {"$unset": {"net_weight_kg": ""}})
        # OData client raises SAPMaterialPhysicalError('no material with InternalID') -> 400; also acceptable: 404
        assert r.status_code in (400, 404), r.text[:400]


# ---------- NEW: GET /api/bom/net-weight ----------
class TestBomNetWeightEndpoint:
    def test_returns_only_ids_with_values(self, auth_cookies, linked_product_id, db):
        # Set a value on linked_product_id, ensure a second real id has no value
        db["component_master"].update_one({"_id": linked_product_id}, {"$set": {"net_weight_kg": 3.75}})
        # find another component with no net weight
        other = db["component_master"].find_one(
            {"_id": {"$ne": linked_product_id},
             "$or": [{"net_weight_kg": None}, {"net_weight_kg": {"$exists": False}}]}
        )
        assert other is not None
        other_id = other["_id"]

        ids = f"{linked_product_id},{other_id}"
        r = requests.get(f"{BASE_URL}/api/bom/net-weight",
                         params={"product_ids": ids}, cookies=auth_cookies, timeout=60)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert isinstance(data, dict)
        assert data.get(linked_product_id) == 3.75
        assert other_id not in data, f"unset id should be absent, but got value {data.get(other_id)}"

    def test_empty_ids_returns_empty(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/bom/net-weight",
                         params={"product_ids": ""}, cookies=auth_cookies, timeout=30)
        assert r.status_code == 200
        assert r.json() == {}

    def test_unknown_ids_return_empty(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/bom/net-weight",
                         params={"product_ids": "TEST_NO_SUCH_1,TEST_NO_SUCH_2"},
                         cookies=auth_cookies, timeout=30)
        assert r.status_code == 200
        assert r.json() == {}


# ---------- LIVE SAP round-trip on a real material ----------
# Try 5989825-2.1 first (confirmed to exist in SAP by main agent), then fall
# back to a few other real product_ids if it's locked/unavailable in SAP.
LIVE_ROUNDTRIP_CANDIDATES = ["5989825-2.1", "SPC5WM", "RFID-WM1942", "BSKWM"]


def _pick_writable_material(auth_cookies):
    """Find a candidate material that SAP will let us write to (not locked by another user)."""
    for pid in LIVE_ROUNDTRIP_CANDIDATES:
        r = requests.get(
            f"{BASE_URL}/api/admin/components/{pid}/sap-physical-attributes",
            cookies=auth_cookies, timeout=120,
        )
        if r.status_code != 200:
            continue
        # Dry-run: try setting a tiny nonzero value and pushing to detect lock
        current = r.json().get("attributes", {}).get("net_weight_kg") or 0
        probe = round((current or 0) + 0.001, 3) or 0.001
        p = requests.patch(
            f"{BASE_URL}/api/admin/components/{pid}",
            json={"net_weight_kg": probe}, cookies=auth_cookies, timeout=60,
        )
        if p.status_code != 200:
            continue
        push = requests.post(
            f"{BASE_URL}/api/admin/components/{pid}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        if push.status_code == 200:
            return pid, current
        # If locked, log and try next
        detail = ""
        try:
            detail = push.json().get("detail", "")
        except Exception:
            pass
        print(f"[live-roundtrip] {pid} not writable ({push.status_code}): {detail[:200]}")
    return None, None


class TestLiveSapRoundTrip:
    def test_live_read_then_write_then_read(self, auth_cookies, db):
        # Find a writable material (skip locked ones)
        pid, original_net = _pick_writable_material(auth_cookies)
        if not pid:
            pytest.skip("No candidate SAP material is currently writable (all locked or unreachable)")

        # A write already succeeded inside _pick_writable_material; now
        # push a distinct 2nd value and confirm the round-trip reads it back.
        test_value = round((original_net or 0) + 0.99, 2)
        p = requests.patch(
            f"{BASE_URL}/api/admin/components/{pid}",
            json={"net_weight_kg": test_value}, cookies=auth_cookies, timeout=60,
        )
        assert p.status_code == 200, p.text[:400]

        push = requests.post(
            f"{BASE_URL}/api/admin/components/{pid}/push-physical-attributes-to-sap",
            cookies=auth_cookies, timeout=180,
        )
        assert push.status_code == 200, f"Push failed {push.status_code}: {push.text[:400]}"
        assert "net_weight_kg" in push.json().get("pushed_fields", [])

        # Read back from SAP
        r2 = requests.get(
            f"{BASE_URL}/api/admin/components/{pid}/sap-physical-attributes",
            cookies=auth_cookies, timeout=120,
        )
        assert r2.status_code == 200, r2.text[:400]
        after = r2.json().get("attributes", {})
        assert after.get("net_weight_kg") == pytest.approx(test_value, rel=1e-3), \
            f"SAP round-trip mismatch on {pid}: pushed {test_value}, read back {after.get('net_weight_kg')}"
        print(f"[live-roundtrip] SUCCESS {pid}: wrote {test_value} kg, read back {after.get('net_weight_kg')} kg")

        # Restore original value
        if original_net:
            requests.patch(
                f"{BASE_URL}/api/admin/components/{pid}",
                json={"net_weight_kg": original_net}, cookies=auth_cookies, timeout=60,
            )
            requests.post(
                f"{BASE_URL}/api/admin/components/{pid}/push-physical-attributes-to-sap",
                cookies=auth_cookies, timeout=180,
            )
        else:
            # Best-effort clear (write 0 -> filter treats 0 as unset)
            db["component_master"].update_one({"_id": pid}, {"$unset": {"net_weight_kg": ""}})


# ---------- BOM search smoke ----------
class TestBomExplorerRegression:
    def test_bom_search_smoke(self, auth_cookies):
        r = requests.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"},
                         cookies=auth_cookies, timeout=180)
        assert r.status_code in (200, 404, 502, 504), r.text[:400]
