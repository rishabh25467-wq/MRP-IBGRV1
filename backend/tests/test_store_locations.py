# Tests for the NEW per-component 'locations' stock breakdown on store_requests
# (Aug 2026 session): production_confirmation_service adds 'locations' to each
# short component, store_approval_service.create_request persists it, and
# submit_issue()/planner_decision() must pass it through untouched.
# Also re-verifies the source-of-supply-options endpoint shape/order for a
# multi-option material (frontend-only auto-pick removal => backend unchanged).
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
API = base_url.rstrip("/") + "/api"
MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

TEST_TAG = "TEST_LOCATIONS"
# New shape (Aug 2026): site-scoped, per-WAREHOUSE + stock_status
LOCS_A = [{"warehouse": "P2-WH01", "stock_status": "Unrestricted", "qty": 100.0},
          {"warehouse": "P2-WH01", "stock_status": "Quality Inspection", "qty": 50.0}]
LOCS_B = [{"warehouse": "P2-WH03", "stock_status": "Unrestricted", "qty": 7.5}]


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def tracker():
    return {"jobs": [], "requests": [], "users": [], "sessions": []}


@pytest.fixture(scope="module", autouse=True)
def cleanup(db, tracker):
    yield
    db["background_jobs"].delete_many({"_id": {"$in": tracker["jobs"]}})
    db["store_requests"].delete_many({"_id": {"$in": tracker["requests"]}})
    db["auth_users"].delete_many({"_id": {"$in": tracker["users"]}})
    db["auth_sessions"].delete_many({"_id": {"$in": tracker["sessions"]}})


@pytest.fixture(scope="module")
def auth_client(db, tracker):
    # unique per pytest-xdist worker: a shared _id would let one worker's
    # module-scoped cleanup delete the auth_user still in use by the other.
    oid = f"{TEST_TAG}-OID-{secrets.token_hex(4)}"
    uid = f"2a94b71e-cd64-4c3d-aede-24eb89ab5fad:{oid}"
    token = "TESTLOC" + secrets.token_urlsafe(24)
    db["auth_users"].replace_one({"_id": uid}, {
        "_id": uid, "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad", "oid": oid,
        "email": "qa.locations@rampgroup.co.in", "name": "QA Locations", "role": "super_admin",
        "allowed_pages": [], "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db["auth_sessions"].insert_one({"_id": token, "user_id": uid,
                                    "expires_at": datetime.now(timezone.utc) + timedelta(hours=8)})
    tracker["users"].append(uid)
    tracker["sessions"].append(token)
    s = requests.Session()
    s.cookies.set("vms_session", token)
    return s


def seed(db, tracker, job_status="waiting_store_approval", req_status="pending", components=None):
    job_id = str(uuid.uuid4())
    req_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    comps = components if components is not None else [
        {"product_id": "TEST_LOC_COMP_A", "description": f"{TEST_TAG} comp A", "unit_of_measure": "EA",
         "required_qty": 200.0, "available_qty": 100.0, "locations": LOCS_A, "issued_qty": None, "shortfall": None},
        {"product_id": "TEST_LOC_COMP_B", "description": f"{TEST_TAG} comp B", "unit_of_measure": "KGM",
         "required_qty": 10.0, "available_qty": 0.0, "locations": LOCS_B, "issued_qty": None, "shortfall": None},
    ]
    db["background_jobs"].insert_one({
        "_id": job_id, "status": job_status, "result": None, "error": None,
        "production_proposal_id": "TEST_PROPOSAL_LOC", "store_request_id": req_id,
        "payload_snapshot": {"material_id": "TEST_MAT_LOC", "site_id": "ZZ99", "quantity": 1.0,
                             "unit_code": "EA", "availability_datetime": None, "actor": TEST_TAG,
                             "logistic_relationship_uuid": None},
        "created_at": now, "updated_at": now,
    })
    db["store_requests"].insert_one({
        "_id": req_id, "job_id": job_id, "production_proposal_id": "TEST_PROPOSAL_LOC",
        "material_id": "TEST_MAT_LOC", "site_id": "ZZ99", "quantity": 1.0, "unit_code": "EA",
        "requester": TEST_TAG, "components": comps, "status": req_status,
        "store_actor": None, "store_decision": None, "planner_actor": None, "planner_decision": None,
        "resolution": None, "created_at": now, "updated_at": now, "resolved_at": None,
    })
    tracker["jobs"].append(job_id)
    tracker["requests"].append(req_id)
    return job_id, req_id


class TestLocationsPassthrough:
    """'locations' must survive every read + every state transition."""

    def test_public_get_returns_locations(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.get(f"{API}/store-requests/{req_id}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "_id" in data and data["_id"] == req_id
        comps = {c["product_id"]: c for c in data["components"]}
        assert comps["TEST_LOC_COMP_A"]["locations"] == LOCS_A
        assert comps["TEST_LOC_COMP_B"]["locations"] == LOCS_B
        # summed available_qty for the target site still present alongside
        assert comps["TEST_LOC_COMP_A"]["available_qty"] == 100.0

    def test_public_queue_returns_locations(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.get(f"{API}/store-requests", timeout=60)
        assert r.status_code == 200, r.text[:300]
        rows = r.json()["requests"] if isinstance(r.json(), dict) else r.json()
        row = next((x for x in rows if x["_id"] == req_id), None)
        assert row is not None, "seeded pending request missing from public queue"
        assert row["components"][0]["locations"] == LOCS_A

    def test_locations_survive_full_issue(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_LOC_COMP_A", "issued_qty": 200.0},
                       {"product_id": "TEST_LOC_COMP_B", "issued_qty": 10.0}],
            "decision": "full", "actor": f"{TEST_TAG} store",
        }, timeout=90)
        assert r.status_code == 200, r.text[:400]
        doc = r.json()["request"] if "request" in r.json() else r.json()
        assert doc["status"] == "resolved"
        comps = {c["product_id"]: c for c in doc["components"]}
        assert comps["TEST_LOC_COMP_A"]["locations"] == LOCS_A
        assert comps["TEST_LOC_COMP_B"]["locations"] == LOCS_B
        assert comps["TEST_LOC_COMP_A"]["issued_qty"] == 200.0
        assert comps["TEST_LOC_COMP_A"]["shortfall"] == 0

    def test_locations_survive_send_to_planner_then_planner_decision(self, db, tracker, auth_client):
        job_id, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_LOC_COMP_A", "issued_qty": 120.0},
                       {"product_id": "TEST_LOC_COMP_B", "issued_qty": 2.0}],
            "decision": "send_to_planner", "actor": f"{TEST_TAG} store",
        }, timeout=90)
        assert r.status_code == 200, r.text[:400]
        # authenticated planner by-job view keeps locations
        by_job = auth_client.get(f"{API}/production-confirmation/store-requests/by-job/{job_id}", timeout=60)
        assert by_job.status_code == 200, by_job.text[:300]
        doc = by_job.json()
        assert doc["status"] == "partial_pending_planner"
        comps = {c["product_id"]: c for c in doc["components"]}
        assert comps["TEST_LOC_COMP_A"]["locations"] == LOCS_A
        assert comps["TEST_LOC_COMP_A"]["shortfall"] == 80.0
        assert comps["TEST_LOC_COMP_B"]["shortfall"] == 8.0
        # planner rejects (avoids kicking off a real 20-min SAP resume loop)
        dec = auth_client.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                               json={"decision": "reject", "actor": f"{TEST_TAG} planner"}, timeout=90)
        assert dec.status_code == 200, dec.text[:400]
        after = requests.get(f"{API}/store-requests/{req_id}", timeout=60).json()
        assert after["status"] == "cancelled"
        assert {c["product_id"]: c["locations"] for c in after["components"]}["TEST_LOC_COMP_A"] == LOCS_A
        job = db["background_jobs"].find_one({"_id": job_id})
        assert job["status"] == "cancelled"

    def test_missing_locations_field_is_tolerated(self, db, tracker):
        """Legacy docs written before this change have no 'locations' key."""
        _, req_id = seed(db, tracker, components=[
            {"product_id": "TEST_LOC_LEGACY", "description": "legacy", "unit_of_measure": "EA",
             "required_qty": 5.0, "available_qty": 1.0, "issued_qty": None, "shortfall": None},
        ])
        r = requests.get(f"{API}/store-requests/{req_id}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["components"][0].get("locations") in (None, [])


class TestSourceOfSupplyOptionsUnchanged:
    """Backend must keep returning ALL options with is_active flags - the
    auto-pick removal is frontend-only."""

    def test_multi_option_material_shape(self, auth_client):
        r = auth_client.get(f"{API}/production-confirmation/source-of-supply-options/HTBS-SPAIN", timeout=180)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data.get("material_uuid")
        opts = data["options"]
        assert len(opts) >= 2, opts
        for o in opts:
            for key in ("production_model_id", "site_id", "logistic_relationship_uuid", "is_active"):
                assert key in o, o
            assert isinstance(o["is_active"], bool)
        assert len({o["logistic_relationship_uuid"] for o in opts}) == len(opts)

    def test_single_option_material_still_one(self, auth_client):
        r = auth_client.get(f"{API}/production-confirmation/source-of-supply-options/MAZ42117272-TA", timeout=180)
        assert r.status_code == 200, r.text[:400]
        assert len(r.json()["options"]) == 1
