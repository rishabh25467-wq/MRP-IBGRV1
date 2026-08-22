"""Rule 2 (Aug 2026): partial-issue -> reopen -> issue-balance lifecycle.

Covers:
  * POST /api/store-requests/{id}/issue  partial + decision=proceed
      -> status "resolved_balance_pending", cumulative issued_qty, shortfall
  * GET  /api/store-requests/balance-pending   (only resolved_balance_pending)
  * GET  /api/store-requests                  (pending queue must EXCLUDE it)
  * reopen round -> status "resolved", resolution "balance_completed"
  * order_resumed double-resume guard
  * planner "approve" with a remaining shortfall -> resolved_balance_pending

All docs are synthetic Mongo seeds with EMPTY `locations`, so the goods
movement path takes the "No stock on file" branch -> no real SAP write.
"""
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing from env and /app/frontend/.env")
API = f"{base_url.rstrip('/')}/api"

MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

TEST_TAG = "TEST_BALANCE_REOPEN"
SITE = "ZZ97"
REQUESTER = "TEST_QA_REQUESTER"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def tracker():
    return {"jobs": [], "requests": []}


@pytest.fixture(scope="module", autouse=True)
def cleanup(db, tracker):
    yield
    if tracker["jobs"]:
        db["background_jobs"].delete_many({"_id": {"$in": tracker["jobs"]}})
    if tracker["requests"]:
        db["store_requests"].delete_many({"_id": {"$in": tracker["requests"]}})


def seed(db, tracker, status="pending", components=None, extra=None):
    job_id = str(uuid.uuid4())
    req_id = f"{TEST_TAG}-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    comps = components if components is not None else [
        {"product_id": "TEST_BAL_A", "description": f"{TEST_TAG} A", "unit_of_measure": "EA",
         "required_qty": 5.0, "available_qty": 3.0, "locations": [], "issued_qty": None, "shortfall": None},
    ]
    db["background_jobs"].insert_one({
        "_id": job_id, "status": "waiting_store_approval", "result": None, "error": None,
        "production_proposal_id": "TEST_PROPOSAL_BAL", "store_request_id": req_id,
        "payload_snapshot": {
            "material_id": "TEST_MAT_BALANCE", "site_id": SITE, "quantity": 1.0, "unit_code": "EA",
            "availability_datetime": None, "actor": REQUESTER, "logistic_relationship_uuid": None,
        },
        "created_at": now, "updated_at": now,
    })
    doc = {
        "_id": req_id, "job_id": job_id, "production_proposal_id": "TEST_PROPOSAL_BAL",
        "material_id": "TEST_MAT_BALANCE", "site_id": SITE, "quantity": 1.0, "unit_code": "EA",
        "requester": REQUESTER, "components": comps, "status": status,
        "store_actor": None, "store_decision": None, "planner_actor": None,
        "planner_decision": None, "resolution": None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    }
    doc.update(extra or {})
    db["store_requests"].insert_one(doc)
    tracker["jobs"].append(job_id)
    tracker["requests"].append(req_id)
    return job_id, req_id


@pytest.fixture(scope="module")
def session_cookie(db):
    """The planner-decision endpoint sits behind Entra SSO - insert a
    synthetic super_admin auth session (same technique as
    tests/_setup_pc_session.py) and use its token as the vms_session cookie."""
    import secrets
    from datetime import timedelta
    user_id = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-BAL-QA"
    token = "TESTBAL" + secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    db.auth_users.replace_one({"_id": user_id}, {
        "_id": user_id, "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad", "oid": "TEST-BAL-QA",
        "email": "qa.balance@rampgroup.co.in", "name": "QA Balance Tester",
        "role": "super_admin", "allowed_pages": [], "created_at": now, "last_login_at": now,
    }, upsert=True)
    db.auth_sessions.insert_one({"_id": token, "user_id": user_id, "expires_at": now + timedelta(hours=4)})
    yield {"vms_session": token}
    db.auth_sessions.delete_one({"_id": token})
    db.auth_users.delete_one({"_id": user_id})


def poll_job(job_id, timeout=90):
    end = time.time() + timeout
    job = None
    while time.time() < end:
        r = requests.get(f"{API}/store-requests/issue-status/{job_id}", timeout=30)
        assert r.status_code == 200, r.text
        job = r.json()
        if job.get("status") in ("done", "failed"):
            return job
        time.sleep(1)
    return job


def get_req(req_id):
    r = requests.get(f"{API}/store-requests/{req_id}", timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


# --- Rule 2 full lifecycle: 3 of 5 now, 2 later ---------------------------
class TestPartialThenReopenLifecycle:
    def test_full_cycle(self, db, tracker):
        job_id, req_id = seed(db, tracker)

        # --- round 1: issue 3 of 5, proceed with partial ---
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 3}],
            "decision": "proceed", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        first = r.json()["request"]
        assert first["status"] == "issuing"
        assert first["components"][0]["issued_qty"] == 3
        assert first["components"][0]["issued_this_round"] == 3
        assert first["components"][0]["shortfall"] == 2

        job = poll_job(r.json()["job_id"])
        assert job["status"] == "done", job
        assert job["result"]["request_status"] == "resolved_balance_pending"

        after1 = get_req(req_id)
        assert after1["status"] == "resolved_balance_pending"
        assert after1["resolution"] == "store_proceeded_partial"
        c = after1["components"][0]
        assert c["issued_qty"] == 3 and c["shortfall"] == 2

        # order pipeline unblocked exactly once
        doc1 = db["store_requests"].find_one({"_id": req_id})
        assert doc1.get("order_resumed") is True, "linked order-creation job should have been resumed once"

        # --- (b) appears on balance-pending, NOT on the pending queue ---
        bp = requests.get(f"{API}/store-requests/balance-pending", timeout=30)
        assert bp.status_code == 200, bp.text
        bp_ids = [x["_id"] for x in bp.json()["requests"]]
        assert req_id in bp_ids
        assert all(x["status"] == "resolved_balance_pending" for x in bp.json()["requests"])

        pending = requests.get(f"{API}/store-requests", timeout=30).json()["requests"]
        assert req_id not in [x["_id"] for x in pending], "resolved_balance_pending must not be in the pending queue"
        assert all(x["status"] in ("pending", "issuing", "partial_pending_planner") for x in pending)

        journal_ids = [x["_id"] for x in requests.get(f"{API}/store-requests/journal", timeout=30).json()["requests"]]
        assert req_id in journal_ids

        # sentinel on the linked order job to prove it is NOT resumed again
        db["background_jobs"].update_one({"_id": job_id}, {"$set": {"status": "TEST_SENTINEL"}})

        # --- (d) round 2: issue the remaining 2 ---
        r2 = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 2}],
            "decision": None, "actor": "QA Store 2",
        }, timeout=60)
        assert r2.status_code == 200, r2.text
        mid = r2.json()["request"]
        assert mid["status"] == "issuing"
        assert mid["components"][0]["issued_qty"] == 5, "issued_qty must be CUMULATIVE"
        assert mid["components"][0]["issued_this_round"] == 2, "only the delta goes to SAP"
        assert mid["components"][0]["shortfall"] == 0

        job2 = poll_job(r2.json()["job_id"])
        assert job2["status"] == "done", job2
        assert job2["result"]["request_status"] == "resolved"

        after2 = get_req(req_id)
        assert after2["status"] == "resolved"
        assert after2["resolution"] == "balance_completed"
        c2 = after2["components"][0]
        assert c2["issued_qty"] == 5 and c2["shortfall"] == 0
        assert after2["store_actor"] == "QA Store 2"

        # gone from the balance-pending tab
        bp2_ids = [x["_id"] for x in requests.get(f"{API}/store-requests/balance-pending", timeout=30).json()["requests"]]
        assert req_id not in bp2_ids

        # --- (e) no duplicate order resume ---
        time.sleep(2)
        linked = db["background_jobs"].find_one({"_id": job_id})
        assert linked["status"] == "TEST_SENTINEL", (
            f"order-creation job was resumed a SECOND time on the reopen round (status={linked['status']})")
        assert db["store_requests"].find_one({"_id": req_id}).get("order_resumed") is True

    def test_multiple_reopens_until_fully_issued(self, db, tracker):
        """3 -> 1 -> 1 across three rounds (user: 'multiple reopens allowed')."""
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 3}], "decision": "proceed", "actor": "QA",
        }, timeout=60)
        assert poll_job(r.json()["job_id"])["status"] == "done"
        assert get_req(req_id)["status"] == "resolved_balance_pending"

        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 1}], "actor": "QA",
        }, timeout=60)
        assert r.status_code == 200, r.text
        assert poll_job(r.json()["job_id"])["status"] == "done"
        mid = get_req(req_id)
        assert mid["status"] == "resolved_balance_pending", mid["status"]
        assert mid["resolution"] == "balance_pending"
        assert mid["components"][0]["issued_qty"] == 4
        assert mid["components"][0]["shortfall"] == 1

        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 1}], "actor": "QA",
        }, timeout=60)
        assert poll_job(r.json()["job_id"])["status"] == "done"
        final = get_req(req_id)
        assert final["status"] == "resolved"
        assert final["resolution"] == "balance_completed"
        assert final["components"][0]["issued_qty"] == 5
        assert final["components"][0]["shortfall"] == 0

    def test_overissue_is_clamped_to_required(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 3}], "decision": "proceed", "actor": "QA",
        }, timeout=60)
        assert poll_job(r.json()["job_id"])["status"] == "done"
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 99}], "actor": "QA",
        }, timeout=60)
        assert r.status_code == 200, r.text
        assert r.json()["request"]["components"][0]["issued_qty"] == 5
        assert poll_job(r.json()["job_id"])["status"] == "done"
        assert get_req(req_id)["status"] == "resolved"

    def test_reopen_rejected_on_fully_resolved_request(self, db, tracker):
        _, req_id = seed(db, tracker, status="resolved",
                         components=[{"product_id": "TEST_BAL_A", "description": "A", "unit_of_measure": "EA",
                                      "required_qty": 5.0, "available_qty": 5.0, "locations": [],
                                      "issued_qty": 5.0, "shortfall": 0.0}],
                         extra={"resolution": "full_issue"})
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 1}], "actor": "QA",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert "not awaiting a stock issue" in r.json()["detail"]

    def test_negative_qty_rejected(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": -2}], "decision": "proceed", "actor": "QA",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert get_req(req_id)["status"] == "pending"


# --- planner approve with a remaining shortfall ---------------------------
class TestPlannerApproveWithShortfall:
    def test_approve_lands_in_balance_pending(self, db, tracker, session_cookie):
        _, req_id = seed(db, tracker, status="partial_pending_planner",
                         components=[{"product_id": "TEST_BAL_A", "description": "A", "unit_of_measure": "EA",
                                      "required_qty": 5.0, "available_qty": 3.0, "locations": [],
                                      "issued_qty": 3.0, "issued_this_round": 3.0, "shortfall": 2.0}],
                         extra={"store_decision": "send_to_planner", "store_actor": "QA Store"})
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "approve", "actor": "QA Planner"}, cookies=session_cookie, timeout=90)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "resolved_balance_pending", body["status"]
        assert body["resolution"] == "planner_approved_partial"

        bp_ids = [x["_id"] for x in requests.get(f"{API}/store-requests/balance-pending", timeout=30).json()["requests"]]
        assert req_id in bp_ids, "planner-approved partial should be reopenable"

        # and it can then be reopened and completed
        r2 = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 2}], "actor": "QA Store 2",
        }, timeout=60)
        assert r2.status_code == 200, r2.text
        assert poll_job(r2.json()["job_id"])["status"] == "done"
        final = get_req(req_id)
        assert final["status"] == "resolved"
        assert final["components"][0]["issued_qty"] == 5

    def test_approve_with_no_shortfall_is_plain_resolved(self, db, tracker, session_cookie):
        _, req_id = seed(db, tracker, status="partial_pending_planner",
                         components=[{"product_id": "TEST_BAL_A", "description": "A", "unit_of_measure": "EA",
                                      "required_qty": 5.0, "available_qty": 5.0, "locations": [],
                                      "issued_qty": 5.0, "shortfall": 0.0}],
                         extra={"store_decision": "send_to_planner", "store_actor": "QA Store"})
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "approve", "actor": "QA Planner"}, cookies=session_cookie, timeout=90)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "resolved"

    def test_reject_cancels(self, db, tracker, session_cookie):
        _, req_id = seed(db, tracker, status="partial_pending_planner",
                         components=[{"product_id": "TEST_BAL_A", "description": "A", "unit_of_measure": "EA",
                                      "required_qty": 5.0, "available_qty": 3.0, "locations": [],
                                      "issued_qty": 3.0, "shortfall": 2.0}],
                         extra={"store_decision": "send_to_planner", "store_actor": "QA Store"})
        r = requests.post(f"{API}/production-confirmation/store-requests/{req_id}/decision",
                          json={"decision": "reject", "actor": "QA Planner"}, cookies=session_cookie, timeout=90)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"
        bp_ids = [x["_id"] for x in requests.get(f"{API}/store-requests/balance-pending", timeout=30).json()["requests"]]
        assert req_id not in bp_ids


# --- no-shortfall regression (iteration_103 flow) -------------------------
class TestFullIssueRegression:
    def test_full_issue_resolves_directly(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_BAL_A", "issued_qty": 5}], "decision": None, "actor": "QA",
        }, timeout=60)
        assert r.status_code == 200, r.text
        assert "job_id" in r.json()
        job = poll_job(r.json()["job_id"])
        assert job["status"] == "done", job
        final = get_req(req_id)
        assert final["status"] == "resolved"
        assert final["resolution"] == "full_issue"
        assert final["issue_id"].startswith(f"{SITE}-I")

    def test_routing_static_paths_not_shadowed(self):
        for path in ("journal", "balance-pending"):
            r = requests.get(f"{API}/store-requests/{path}", timeout=30)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
            assert "requests" in r.json()
        r = requests.get(f"{API}/store-requests/NO_SUCH_ID_XYZ", timeout=30)
        assert r.status_code == 404

    def test_no_mongo_object_id_leaked(self):
        for path in ("store-requests", "store-requests/journal", "store-requests/balance-pending"):
            data = requests.get(f"{API}/{path}", timeout=30).json()["requests"]
            for doc in data[:20]:
                assert "$oid" not in str(doc.get("_id"))
