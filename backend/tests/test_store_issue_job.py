"""Store Approval - async background "Issue Stock" job (Aug 2026 conversion).

Covers the new split flow:
  POST /api/store-requests/{id}/issue  -> returns {job_id, request} instantly,
                                          request.status == "issuing"
  GET  /api/store-requests/issue-status/{job_id} -> pollable job doc
  GET  /api/store-requests/{id}        -> final resolved state

All docs are synthetic Mongo seeds with NO `locations` data, so the goods
movement path takes the fast "No stock on file" branch -> no real SAP write.
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

TEST_TAG = "TEST_STORE_ISSUE_JOB"
SITE = "ZZ98"


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


def seed(db, tracker, status="pending", components=None):
    job_id = str(uuid.uuid4())
    req_id = f"{TEST_TAG}-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    comps = components or [
        {"product_id": "TEST_ISS_A", "description": f"{TEST_TAG} A", "unit_of_measure": "EA",
         "required_qty": 10.0, "available_qty": 3.0, "locations": [], "issued_qty": None, "shortfall": None},
        {"product_id": "TEST_ISS_B", "description": f"{TEST_TAG} B", "unit_of_measure": "KG",
         "required_qty": 5.0, "available_qty": 0.0, "locations": [], "issued_qty": None, "shortfall": None},
    ]
    db["background_jobs"].insert_one({
        "_id": job_id, "status": "waiting_store_approval", "result": None, "error": None,
        "production_proposal_id": "TEST_PROPOSAL_ISS", "store_request_id": req_id,
        "payload_snapshot": {
            "material_id": "TEST_MAT_ISSUE_JOB", "site_id": SITE, "quantity": 1.0, "unit_code": "EA",
            "availability_datetime": None, "actor": TEST_TAG, "logistic_relationship_uuid": None,
        },
        "created_at": now, "updated_at": now,
    })
    db["store_requests"].insert_one({
        "_id": req_id, "job_id": job_id, "production_proposal_id": "TEST_PROPOSAL_ISS",
        "material_id": "TEST_MAT_ISSUE_JOB", "site_id": SITE, "quantity": 1.0, "unit_code": "EA",
        "requester": TEST_TAG, "components": comps, "status": status,
        "store_actor": None, "store_decision": None, "planner_actor": None,
        "planner_decision": None, "resolution": None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    })
    tracker["jobs"].append(job_id)
    tracker["requests"].append(req_id)
    return job_id, req_id


def poll_job(job_id, timeout=90):
    """Poll the new status endpoint until terminal; return (final_job, snapshots)."""
    snapshots = []
    end = time.time() + timeout
    while time.time() < end:
        r = requests.get(f"{API}/store-requests/issue-status/{job_id}", timeout=30)
        assert r.status_code == 200, r.text
        job = r.json()
        snapshots.append(job)
        if job.get("status") in ("done", "failed"):
            return job, snapshots
        time.sleep(1)
    return snapshots[-1] if snapshots else None, snapshots


# --- POST /issue returns instantly ---------------------------------------
class TestIssueReturnsImmediately:
    def test_response_is_fast_and_shaped_correctly(self, db, tracker):
        _, req_id = seed(db, tracker)
        start = time.time()
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_ISS_A", "issued_qty": 10},
                       {"product_id": "TEST_ISS_B", "issued_qty": 5}],
            "decision": None, "actor": "QA Store",
        }, timeout=60)
        elapsed = time.time() - start
        assert r.status_code == 200, r.text
        assert elapsed < 10, f"issue endpoint took {elapsed:.1f}s - should be near-instant"
        body = r.json()
        assert "job_id" in body and isinstance(body["job_id"], str)
        req = body["request"]
        assert req["status"] == "issuing"
        assert req["store_actor"] == "QA Store"
        comps = {c["product_id"]: c for c in req["components"]}
        assert comps["TEST_ISS_A"]["issued_qty"] == 10
        assert comps["TEST_ISS_A"]["shortfall"] == 0
        assert comps["TEST_ISS_B"]["issued_qty"] == 5
        assert comps["TEST_ISS_B"]["shortfall"] == 0
        # goods_movement not yet performed at this point
        assert all(c.get("goods_movement") is None for c in req["components"])
        assert req.get("issue_id") is None

        # job is pollable and reaches done
        job, snaps = poll_job(body["job_id"])
        assert job["status"] == "done", job
        assert job["result"]["request_status"] == "resolved"
        assert job["result"]["request_id"] == req_id
        assert any(s.get("progress_total") == 2 for s in snaps)

        # final state
        final = requests.get(f"{API}/store-requests/{req_id}", timeout=30).json()
        assert final["status"] == "resolved"
        assert final["resolution"] == "full_issue"
        assert final["issue_id"].startswith(f"{SITE}-I"), final["issue_id"]
        assert len(final["issue_id"]) == len(SITE) + 8
        assert final["target_logistics_area_id"] == f"{SITE}/{SITE}-SFG"
        for c in final["components"]:
            gm = c["goods_movement"]
            assert gm is not None, c["product_id"]
            assert gm["attempted"] is False
            assert gm["ok"] is False
            assert "No stock on file" in gm["reason"]

    def test_zero_issued_skips_movement_entirely(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_ISS_A", "issued_qty": 0},
                       {"product_id": "TEST_ISS_B", "issued_qty": 0}],
            "decision": "proceed", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        job, _ = poll_job(r.json()["job_id"])
        assert job["status"] == "done", job
        final = requests.get(f"{API}/store-requests/{req_id}", timeout=30).json()
        # Rule 2 (Aug 2026): a remaining shortfall no longer dead-ends as
        # "resolved" - it stays reopenable as "resolved_balance_pending".
        assert final["status"] == "resolved_balance_pending"
        assert final["resolution"] == "store_proceeded_partial"
        assert all(c.get("goods_movement") is None for c in final["components"])
        assert all(c.get("issued_from_warehouse") is None for c in final["components"])


# --- decision branches ----------------------------------------------------
class TestIssueDecisionBranches:
    def test_partial_send_to_planner(self, db, tracker):
        job_id, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_ISS_A", "issued_qty": 2},
                       {"product_id": "TEST_ISS_B", "issued_qty": 5}],
            "decision": "send_to_planner", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        assert r.json()["request"]["status"] == "issuing"
        assert r.json()["request"]["store_decision"] == "send_to_planner"
        job, _ = poll_job(r.json()["job_id"])
        assert job["status"] == "done", job
        assert job["result"]["request_status"] == "partial_pending_planner"
        final = requests.get(f"{API}/store-requests/{req_id}", timeout=30).json()
        assert final["status"] == "partial_pending_planner"
        assert final["resolution"] is None
        assert final["issue_id"].startswith(f"{SITE}-I")
        comps = {c["product_id"]: c for c in final["components"]}
        assert comps["TEST_ISS_A"]["shortfall"] == 8
        # the linked order-creation job is parked, not resumed
        linked = db["background_jobs"].find_one({"_id": job_id})
        assert linked["status"] == "partial_pending_planner"
        assert linked["store_request_id"] == req_id

    def test_partial_proceed_resumes_order_job(self, db, tracker):
        job_id, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_ISS_A", "issued_qty": 4},
                       {"product_id": "TEST_ISS_B", "issued_qty": 1}],
            "decision": "proceed", "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 200, r.text
        job, _ = poll_job(r.json()["job_id"])
        assert job["status"] == "done", job
        final = requests.get(f"{API}/store-requests/{req_id}", timeout=30).json()
        assert final["status"] == "resolved_balance_pending"  # Rule 2: reopenable
        assert final["resolution"] == "store_proceeded_partial"
        # order-creation job must be picked back up (moves off waiting_store_approval)
        end = time.time() + 25
        statuses = set()
        while time.time() < end:
            statuses.add(db["background_jobs"].find_one({"_id": job_id})["status"])
            if statuses - {"waiting_store_approval"}:
                break
            time.sleep(1)
        assert statuses - {"waiting_store_approval"}, f"order job never resumed: {statuses}"


# --- validation / double-submit ------------------------------------------
class TestIssueValidation:
    def test_second_submit_while_issuing_is_400(self, db, tracker):
        _, req_id = seed(db, tracker, status="issuing")
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_ISS_A", "issued_qty": 10},
                       {"product_id": "TEST_ISS_B", "issued_qty": 5}],
            "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert "not awaiting a stock issue" in r.json()["detail"]

    def test_partial_without_decision_is_400_and_stays_pending(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": "TEST_ISS_A", "issued_qty": 1},
                       {"product_id": "TEST_ISS_B", "issued_qty": 1}],
            "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert requests.get(f"{API}/store-requests/{req_id}", timeout=30).json()["status"] == "pending"

    def test_blank_actor_is_400(self, db, tracker):
        _, req_id = seed(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [], "decision": "proceed", "actor": "  ",
        }, timeout=60)
        assert r.status_code == 400, r.text

    def test_unknown_request_404(self):
        r = requests.post(f"{API}/store-requests/{uuid.uuid4()}/issue", json={
            "issued": [], "decision": None, "actor": "QA Store",
        }, timeout=60)
        assert r.status_code == 404, r.text

    def test_unknown_job_id_404(self):
        r = requests.get(f"{API}/store-requests/issue-status/{uuid.uuid4()}", timeout=30)
        assert r.status_code == 404, r.text


# --- queue / journal regression ------------------------------------------
class TestQueueAndJournal:
    def test_issuing_request_appears_in_queue(self, db, tracker):
        _, req_id = seed(db, tracker, status="issuing")
        rows = requests.get(f"{API}/store-requests", timeout=30).json()["requests"]
        row = next((x for x in rows if x["_id"] == req_id), None)
        assert row is not None, "an 'issuing' request must stay visible in the pending queue"
        assert row["status"] == "issuing"
        assert "_id" in row and isinstance(row["_id"], str)

    def test_journal_shape(self, db, tracker):
        _, req_id = seed(db, tracker, status="pending")
        r = requests.get(f"{API}/store-requests/journal", timeout=60)
        assert r.status_code == 200, r.text
        rows = r.json()["requests"]
        assert isinstance(rows, list) and rows
        row = next((x for x in rows if x["_id"] == req_id), None)
        assert row is not None
        for key in ("status", "site_id", "material_id", "components", "created_at"):
            assert key in row, key
