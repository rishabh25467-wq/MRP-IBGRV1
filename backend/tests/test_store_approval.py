"""Store Approval workflow (Aug 2026) - public /api/store-requests* endpoints
and authenticated /api/production-confirmation/store-requests* endpoints.

Uses synthetic Mongo-seeded background_jobs + store_requests docs (no live SAP
proposal creation) plus a synthetic auth_users/auth_sessions pair for the
authenticated planner endpoints. All seeded docs are removed in teardown.
"""
import os
import secrets
import time
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
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

TEST_TAG = "TEST_STORE_APPROVAL"


@pytest.fixture(scope="session")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="session")
def tracker():
    return {"jobs": [], "requests": [], "users": [], "sessions": []}


@pytest.fixture(scope="session", autouse=True)
def cleanup(db, tracker):
    yield
    if tracker["jobs"]:
        db["background_jobs"].delete_many({"_id": {"$in": tracker["jobs"]}})
    if tracker["requests"]:
        db["store_requests"].delete_many({"_id": {"$in": tracker["requests"]}})
    if tracker["users"]:
        db["auth_users"].delete_many({"_id": {"$in": tracker["users"]}})
    if tracker["sessions"]:
        db["auth_sessions"].delete_many({"_id": {"$in": tracker["sessions"]}})
    # NOTE: resumed jobs keep an asyncio task polling SAP for up to 20 min -
    # restart the backend manually after the suite if you want them stopped.


def seed(db, tracker, status="pending", components=None, with_snapshot=True):
    """Insert a paused background job + linked store_request."""
    job_id = str(uuid.uuid4())
    req_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    comps = components or [
        {"product_id": "TEST_COMP_A", "description": f"{TEST_TAG} comp A", "unit_of_measure": "EA",
         "required_qty": 10.0, "available_qty": 4.0, "issued_qty": None, "shortfall": None},
        {"product_id": "TEST_COMP_B", "description": f"{TEST_TAG} comp B", "unit_of_measure": "EA",
         "required_qty": 5.0, "available_qty": 0.0, "issued_qty": None, "shortfall": None},
    ]
    snapshot = {
        "material_id": "TEST_MAT_STORE_APPROVAL", "site_id": "ZZ99", "quantity": 1.0,
        "unit_code": "EA", "availability_datetime": None, "actor": TEST_TAG,
        "logistic_relationship_uuid": None,
    }
    job_doc = {
        "_id": job_id,
        "status": "waiting_store_approval" if status == "pending" else "partial_pending_planner",
        "result": None, "error": None,
        "production_proposal_id": "TEST_PROPOSAL_0001",
        "store_request_id": req_id,
        "created_at": now, "updated_at": now,
    }
    if with_snapshot:
        job_doc["payload_snapshot"] = snapshot
    db["background_jobs"].insert_one(job_doc)
    db["store_requests"].insert_one({
        "_id": req_id, "job_id": job_id, "production_proposal_id": "TEST_PROPOSAL_0001",
        "material_id": snapshot["material_id"], "site_id": snapshot["site_id"],
        "quantity": 1.0, "unit_code": "EA", "requester": TEST_TAG,
        "components": comps, "status": status,
        "store_actor": None, "store_decision": None, "planner_actor": None,
        "planner_decision": None, "resolution": None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    })
    tracker["jobs"].append(job_id)
    tracker["requests"].append(req_id)
    return job_id, req_id


def job_status(db, job_id):
    doc = db["background_jobs"].find_one({"_id": job_id})
    return doc and doc.get("status")


def wait_for_job_status(db, job_id, expected, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if job_status(db, job_id) == expected:
            return True
        time.sleep(1)
    return False


@pytest.fixture(scope="session")
def pc_cookie(db, tracker):
    """Synthetic signed-in user WITH production_confirmation access."""
    uid = f"{TEST_TAG}-tid:{TEST_TAG}-oid"
    token = secrets.token_urlsafe(32)
    db["auth_users"].replace_one({"_id": uid}, {
        "_id": uid, "tid": f"{TEST_TAG}-tid", "oid": f"{TEST_TAG}-oid",
        "email": "test_store_approval@example.test", "name": "QA Planner", "role": "user",
        "allowed_pages": ["production_confirmation"],
        "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db["auth_sessions"].insert_one({"_id": token, "user_id": uid,
                                   "expires_at": datetime.now(timezone.utc) + timedelta(days=1)})
    tracker["users"].append(uid)
    tracker["sessions"].append(token)
    return {"vms_session": token}


@pytest.fixture(scope="session")
def nopage_cookie(db, tracker):
    """Synthetic signed-in user WITHOUT production_confirmation access."""
    uid = f"{TEST_TAG}-tid2:{TEST_TAG}-oid2"
    token = secrets.token_urlsafe(32)
    db["auth_users"].replace_one({"_id": uid}, {
        "_id": uid, "tid": f"{TEST_TAG}-tid2", "oid": f"{TEST_TAG}-oid2",
        "email": "test_store_approval2@example.test", "name": "QA NoAccess", "role": "user",
        "allowed_pages": ["inventory"],
        "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db["auth_sessions"].insert_one({"_id": token, "user_id": uid,
                                   "expires_at": datetime.now(timezone.utc) + timedelta(days=1)})
    tracker["users"].append(uid)
    tracker["sessions"].append(token)
    return {"vms_session": token}


# --- PUBLIC endpoints (no auth) -------------------------------------------
class TestPublicStoreRequestReads:
    def test_list_is_public_and_returns_pending_only(self, db, tracker):
        _, req_id = seed(db, tracker)
        _, planner_req = seed(db, tracker, status="partial_pending_planner")
        r = requests.get(f"{API}/store-requests", timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "requests" in body and isinstance(body["requests"], list)
        ids = [x["_id"] for x in body["requests"]]
        assert req_id in ids
        assert planner_req in ids
        assert all(x["status"] in ("pending", "partial_pending_planner") for x in body["requests"])

    def test_detail_is_public_with_components(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.get(f"{API}/store-requests/{req_id}", timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["_id"] == req_id
        assert d["status"] == "pending"
        assert len(d["components"]) == 2
        assert d["components"][0]["required_qty"] == 10.0
        assert d["material_id"] == "TEST_MAT_STORE_APPROVAL"

    def test_detail_unknown_id_404(self):
        r = requests.get(f"{API}/store-requests/{uuid.uuid4()}", timeout=60)
        assert r.status_code == 404, r.text


# --- POST /issue ----------------------------------------------------------
class TestStoreIssue:
    def test_full_issue_resolves_and_resumes_job(self, db, tracker):
        job_id, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 10},
                       {"product_id": "TEST_COMP_B", "issued_qty": 5}],
            "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "resolved"
        assert d["resolution"] == "full_issue"
        assert d["store_actor"] == "QA Store"
        assert all(c["shortfall"] == 0 for c in d["components"])
        # persisted
        got = requests.get(f"{API}/store-requests/{req_id}", timeout=60).json()
        assert got["status"] == "resolved"
        # job resumed
        assert wait_for_job_status(db, job_id, "waiting_for_order"), job_status(db, job_id)
        # resolved requests drop off the public queue
        queue = requests.get(f"{API}/store-requests", timeout=60).json()["requests"]
        assert req_id not in [x["_id"] for x in queue]

    def test_partial_proceed_resolves_and_resumes_job(self, db, tracker):
        job_id, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 4},
                       {"product_id": "TEST_COMP_B", "issued_qty": 1}],
            "decision": "proceed", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "resolved"
        assert d["resolution"] == "store_proceeded_partial"
        comps = {c["product_id"]: c for c in d["components"]}
        assert comps["TEST_COMP_A"]["issued_qty"] == 4
        assert comps["TEST_COMP_A"]["shortfall"] == 6
        assert comps["TEST_COMP_B"]["shortfall"] == 4
        assert wait_for_job_status(db, job_id, "waiting_for_order"), job_status(db, job_id)

    def test_partial_send_to_planner(self, db, tracker):
        job_id, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 2},
                       {"product_id": "TEST_COMP_B", "issued_qty": 5}],
            "decision": "send_to_planner", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "partial_pending_planner"
        assert d["resolution"] is None
        assert job_status(db, job_id) == "partial_pending_planner"
        job = db["background_jobs"].find_one({"_id": job_id})
        assert job["store_request_id"] == req_id

    def test_partial_without_decision_is_400(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 1},
                       {"product_id": "TEST_COMP_B", "issued_qty": 1}],
            "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert requests.get(f"{API}/store-requests/{req_id}", timeout=60).json()["status"] == "pending"

    def test_missing_component_treated_as_zero_issued(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 10}],
            "decision": "send_to_planner", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        comps = {c["product_id"]: c for c in r.json()["components"]}
        assert comps["TEST_COMP_B"]["issued_qty"] == 0
        assert comps["TEST_COMP_B"]["shortfall"] == 5

    def test_blank_actor_is_400(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 10},
                       {"product_id": "TEST_COMP_B", "issued_qty": 5}],
            "decision": None, "actor": "   ",
        }, timeout=60)
        assert r.status_code == 400, r.text

    def test_double_submit_on_non_pending_is_400(self, db, tracker):
        _, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_COMP_A", "issued_qty": 10},
                       {"product_id": "TEST_COMP_B", "issued_qty": 5}],
            "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert "no longer pending" in r.json()["detail"]

    def test_issue_unknown_request_404(self):
        r = requests.post(f"{API}/store-requests/{uuid.uuid4()}/issue", json={
            "issued": [], "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 404, r.text


# --- AUTHENTICATED planner endpoints -------------------------------------
class TestPlannerEndpointsAuth:
    def test_by_job_requires_login(self, db, tracker):
        job_id, _ = seed(db, tracker, status="partial_pending_planner")
        r = requests.get(f"{API}/production-confirmation/store-requests/by-job/{job_id}", timeout=60)
        assert r.status_code == 401, r.text

    def test_by_job_requires_page_permission(self, db, tracker, nopage_cookie):
        job_id, _ = seed(db, tracker, status="partial_pending_planner")
        r = requests.get(f"{API}/production-confirmation/store-requests/by-job/{job_id}",
                         cookies=nopage_cookie, timeout=60)
        assert r.status_code == 403, r.text

    def test_by_job_returns_linked_request(self, db, tracker, pc_cookie):
        job_id, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.get(f"{API}/production-confirmation/store-requests/by-job/{job_id}",
                         cookies=pc_cookie, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["_id"] == req_id
        assert d["job_id"] == job_id
        assert len(d["components"]) == 2

    def test_by_job_unknown_404(self, pc_cookie):
        r = requests.get(f"{API}/production-confirmation/store-requests/by-job/{uuid.uuid4()}",
                         cookies=pc_cookie, timeout=60)
        assert r.status_code == 404, r.text

    def test_decision_requires_login(self, db, tracker):
        _, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "approve", "actor": "QA"}, timeout=60)
        assert r.status_code == 401, r.text
        assert db["store_requests"].find_one({"_id": req_id})["status"] == "partial_pending_planner"


class TestPlannerDecision:
    def test_approve_resolves_and_resumes(self, db, tracker, pc_cookie):
        job_id, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "approve", "actor": "QA Planner"},
                          cookies=pc_cookie, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "resolved"
        assert d["resolution"] == "planner_approved_partial"
        assert d["planner_actor"] == "QA Planner"
        assert wait_for_job_status(db, job_id, "waiting_for_order"), job_status(db, job_id)

    def test_reject_cancels_request_and_job(self, db, tracker, pc_cookie):
        job_id, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "reject", "actor": "QA Planner"},
                          cookies=pc_cookie, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "cancelled"
        assert d["planner_decision"] == "reject"
        assert d["resolution"] is None
        job = db["background_jobs"].find_one({"_id": job_id})
        assert job["status"] == "cancelled"
        assert job["result"]["released"] is False
        assert job["result"]["production_order_id"] is None
        assert "no API to delete a Production Proposal" in job["result"]["note"]

    def test_decision_on_pending_request_is_400(self, db, tracker, pc_cookie):
        _, req_id = seed(db, tracker, status="pending")
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "approve", "actor": "QA Planner"},
                          cookies=pc_cookie, timeout=60)
        assert r.status_code == 400, r.text
        assert "not awaiting a planner decision" in r.json()["detail"]

    def test_invalid_decision_value_is_400(self, db, tracker, pc_cookie):
        _, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "maybe", "actor": "QA Planner"},
                          cookies=pc_cookie, timeout=60)
        assert r.status_code == 400, r.text
        assert db["store_requests"].find_one({"_id": req_id})["status"] == "partial_pending_planner"

    def test_blank_actor_is_400(self, db, tracker, pc_cookie):
        _, req_id = seed(db, tracker, status="partial_pending_planner")
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "approve", "actor": ""},
                          cookies=pc_cookie, timeout=60)
        assert r.status_code == 400, r.text

    def test_decision_unknown_request_404(self, pc_cookie):
        r = requests.post(f"{API}/production-confirmation/store-requests/{uuid.uuid4()}/decision",
                          json={"decision": "approve", "actor": "QA Planner"},
                          cookies=pc_cookie, timeout=60)
        assert r.status_code == 404, r.text
