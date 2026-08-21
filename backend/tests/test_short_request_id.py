"""Aug 2026 session: short 'SR-XXXXXX' store request IDs + component
availability `locations` breakdown (site/warehouse/stock_status)."""
import os
import re
import sys

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")
import store_approval_service  # noqa: E402

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")

backend_env = dotenv_values("/app/backend/.env")
MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

SHORT_ID_RE = re.compile(r"^SR-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}$")


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def created_ids():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(db, created_ids):
    yield
    if created_ids:
        db[store_approval_service.COLLECTION].delete_many({"_id": {"$in": created_ids}})


# --- store_approval_service short ID generation ---
class TestShortIdGeneration:
    def test_generate_short_id_format(self):
        ids = [store_approval_service._generate_short_id() for _ in range(200)]
        for i in ids:
            assert SHORT_ID_RE.match(i), f"bad id format: {i}"
        # ambiguous chars excluded
        assert not any(ch in i[3:] for i in ids for ch in "01OIL")
        # reasonable entropy - no mass collisions
        assert len(set(ids)) > 190

    def test_create_request_uses_short_id(self, db, created_ids):
        payload = {"material_id": "TEST_MAT_001", "site_id": "P2", "quantity": 10.0, "unit_code": "EA"}
        short_components = [{
            "product_id": "TEST_COMP_1", "description": "TEST comp", "unit_of_measure": "KGM",
            "required_qty": 5.0, "available_qty": 1.0,
            "locations": [{"warehouse": "RM", "stock_status": "Unrestricted", "qty": 1.0}],
        }]
        doc = store_approval_service.create_request(
            db, "TEST_JOB_SHORTID", payload, "TEST_PROP_1", short_components, "TEST_QA",
        )
        created_ids.append(doc["_id"])
        assert SHORT_ID_RE.match(doc["_id"]), f"create_request returned non-short id: {doc['_id']}"
        assert doc["status"] == "pending"

        # persisted + retrievable by the short id
        fetched = store_approval_service.get_request(db, doc["_id"])
        assert fetched is not None
        assert fetched["material_id"] == "TEST_MAT_001"
        assert fetched["components"][0]["locations"][0]["warehouse"] == "RM"

    def test_short_id_reachable_via_api(self, db, created_ids):
        payload = {"material_id": "TEST_MAT_002", "site_id": "P2", "quantity": 3.0, "unit_code": "EA"}
        doc = store_approval_service.create_request(
            db, "TEST_JOB_SHORTID2", payload, "TEST_PROP_2",
            [{"product_id": "TEST_COMP_2", "required_qty": 2.0, "available_qty": 0.0, "locations": []}],
            "TEST_QA",
        )
        created_ids.append(doc["_id"])
        r = requests.get(f"{BASE_URL}/api/store-requests/{doc['_id']}", timeout=30)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["_id"] == doc["_id"]
        assert body["material_id"] == "TEST_MAT_002"


# --- public /storeapproval queue + journal endpoints ---
class TestStoreRequestEndpoints:
    def test_queue_and_journal_include_ids(self, created_ids):
        q = requests.get(f"{BASE_URL}/api/store-requests", timeout=30)
        assert q.status_code == 200, q.text[:300]
        qreqs = q.json()["requests"]
        assert isinstance(qreqs, list)
        for r in qreqs:
            assert isinstance(r["_id"], str) and r["_id"]
            assert r["status"] in ("pending", "partial_pending_planner")

        j = requests.get(f"{BASE_URL}/api/store-requests/journal", timeout=30)
        assert j.status_code == 200, j.text[:300]
        jreqs = j.json()["requests"]
        ids = {r["_id"] for r in jreqs}
        assert set(created_ids).issubset(ids), "newly created short-id requests missing from journal"
        # legacy long uuid ids must still be served (backward compat)
        legacy = [i for i in ids if len(i) == 36 and i.count("-") == 4]
        print(f"legacy uuid4 ids present: {len(legacy)}, short ids present: {len([i for i in ids if SHORT_ID_RE.match(i)])}")

    def test_unknown_request_id_404(self):
        r = requests.get(f"{BASE_URL}/api/store-requests/SR-ZZZZZZ", timeout=30)
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"


@pytest.fixture(scope="module")
def auth_cookies():
    """Synthetic Entra ID session (app-wide SSO can't be automated)."""
    import subprocess
    token = subprocess.check_output(
        [sys.executable, "/app/backend/tests/_setup_pc_session.py"], text=True,
    ).strip()
    return {"vms_session": token}


# --- component availability locations breakdown ---
class TestComponentAvailabilityLocations:
    def test_requires_auth(self):
        r = requests.get(
            f"{BASE_URL}/api/production-confirmation/component-availability",
            params={"main_output_product": "X", "confirmed_quantity": 1, "site_id": "P2"},
            timeout=30,
        )
        assert r.status_code == 401

    def test_locations_present_and_shaped(self, db, auth_cookies):
        # find a cached BOM with at least one active component
        bom = db["bom_node_cache"].find_one({"groups.items.active": True})
        if not bom:
            pytest.skip("no cached BOM with active components in bom_node_cache")
        r = requests.get(
            f"{BASE_URL}/api/production-confirmation/component-availability",
            params={"main_output_product": bom["_id"], "confirmed_quantity": 1, "site_id": "P2"},
            cookies=auth_cookies,
            timeout=60,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data["checked"] is True
        assert isinstance(data["components"], list)
        loc_count = 0
        for c in data["components"]:
            assert "locations" in c, f"component {c['product_id']} missing locations field"
            assert isinstance(c["locations"], list)
            for loc in c["locations"]:
                assert set(loc.keys()) == {"warehouse", "stock_status", "qty"}, loc
                assert isinstance(loc["qty"], (int, float))
                loc_count += 1
            if c["locations"]:
                assert c["available_qty"] == pytest.approx(sum(l["qty"] for l in c["locations"]), abs=0.01)
        print(f"BOM {bom['_id']}: {len(data['components'])} components, {loc_count} location rows")

    def test_zero_quantity_and_bad_bom(self, auth_cookies):
        r = requests.get(
            f"{BASE_URL}/api/production-confirmation/component-availability",
            params={"main_output_product": "TEST_NO_SUCH_PRODUCT", "confirmed_quantity": 5, "site_id": "P2"},
            cookies=auth_cookies,
            timeout=60,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data["checked"] is False
        assert data["components"] == []
        assert data["reason"]
