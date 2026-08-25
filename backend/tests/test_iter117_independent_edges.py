"""Independent verification (iteration_117) of the 3 new Production
Confirmation features - edge cases NOT covered by
test_recovery_and_byproduct_enforcement.py:

* auth enforcement (no vms_session cookie) on all 3 new endpoints
* retry-wip-clearing touches ONLY the most recent history doc for the lot
* resume on a still-running / already-done job (duplicate-pipeline risk)
"""
import os
import secrets
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
TIMEOUT = 120

LOT = "TEST_QA_LOT_117B"


@pytest.fixture(scope="module")
def mongo_db():
    url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    return MongoClient(url)[name]


@pytest.fixture(scope="module")
def client(mongo_db):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    oid = f"TEST-QA-OID-117B-{worker}"
    user_id = f"2a94b71e-cd64-4c3d-aede-24eb89ab5fad:{oid}"
    token = "TESTQAB" + secrets.token_urlsafe(24)
    mongo_db.auth_users.replace_one(
        {"_id": user_id},
        {"_id": user_id, "tid": user_id.split(":")[0], "oid": oid,
         "email": "qa117b.tester@rampgroup.co.in", "name": "QA Tester 117B",
         "role": "super_admin", "allowed_pages": [],
         "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc)},
        upsert=True)
    mongo_db.auth_sessions.insert_one(
        {"_id": token, "user_id": user_id,
         "expires_at": datetime.now(timezone.utc) + timedelta(hours=2)})
    s = requests.Session()
    s.cookies.set("vms_session", token)
    yield s
    mongo_db.auth_sessions.delete_one({"_id": token})
    mongo_db.auth_users.delete_one({"_id": user_id})


# ---------- auth enforcement ----------
class TestAuthEnforcement:
    def test_confirm_requires_session(self):
        r = requests.post(f"{API}/production-confirmation/confirm",
                          json={"production_lot_id": "X", "actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code in (401, 403, 422), r.status_code

    def test_retry_wip_requires_session(self):
        r = requests.post(f"{API}/production-confirmation/retry-wip-clearing",
                          json={"production_lot_id": LOT, "site_id": "P1", "actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code in (401, 403), f"unauthenticated retry-wip returned {r.status_code}: {r.text[:200]}"

    def test_resume_requires_session(self):
        r = requests.post(f"{API}/production-confirmation/create-and-release-order/whatever/resume",
                          json={"actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code in (401, 403), f"unauthenticated resume returned {r.status_code}: {r.text[:200]}"


# ---------- retry-wip-clearing targets only the latest history doc ----------
class TestRetryTargetsLatestOnly:
    @pytest.fixture(autouse=True)
    def seed(self, mongo_db):
        col = mongo_db["production_confirmation_history"]
        col.delete_many({"production_lot_id": LOT})
        now = datetime.now(timezone.utc)
        col.insert_many([
            {"actor": "QA", "production_lot_id": LOT, "main_output_product": "TEST_QA_X",
             "confirmed_quantity": 1, "success": True, "logs": [],
             "wip_clearing": {"success": True, "log": "OLD-DOC-MUST-NOT-CHANGE"},
             "at": now - timedelta(hours=2)},
            {"actor": "QA", "production_lot_id": LOT, "main_output_product": "TEST_QA_X",
             "confirmed_quantity": 2, "success": True, "logs": [],
             "wip_clearing": {"success": False, "log": "seeded failure"},
             "at": now},
        ])
        yield
        col.delete_many({"production_lot_id": LOT})

    def test_only_latest_doc_updated(self, client, mongo_db):
        r = client.post(f"{API}/production-confirmation/retry-wip-clearing",
                        json={"production_lot_id": LOT, "site_id": "P1", "actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        new_wip = r.json()["wip_clearing"]
        col = mongo_db["production_confirmation_history"]
        docs = list(col.find({"production_lot_id": LOT}).sort("at", 1))
        assert len(docs) == 2, "retry must not insert a new history doc"
        assert docs[0]["wip_clearing"]["log"] == "OLD-DOC-MUST-NOT-CHANGE"
        assert docs[1]["wip_clearing"].get("success") == new_wip.get("success")
        assert docs[1]["wip_clearing"] != {"success": False, "log": "seeded failure"}
        # main confirmation data untouched
        assert docs[1]["confirmed_quantity"] == 2
        assert docs[1]["success"] is True


# ---------- resume guardrails on non-failed jobs ----------
class TestResumeStateGuards:
    JOBS = []

    def _seed(self, mongo_db, status):
        job_id = "TEST_QA_JOB_117B_" + secrets.token_hex(6)
        mongo_db["background_jobs"].insert_one({
            "_id": job_id, "created_at": datetime.now(timezone.utc), "status": status,
            "result": {"reason": "pipeline_error", "production_proposal_id": "TEST_QA_PROP_117B"},
            "payload_snapshot": {"material_id": "TEST_QA_MASS_117", "site_id": "P1",
                                 "quantity": 5, "unit_code": "EA", "actor": "QA"},
        })
        self.JOBS.append(job_id)
        return job_id

    @pytest.fixture(autouse=True)
    def cleanup(self, mongo_db):
        yield
        if self.JOBS:
            mongo_db["background_jobs"].delete_many({"_id": {"$in": self.JOBS}})
            self.JOBS.clear()

    def test_resume_on_running_job(self, client, mongo_db):
        """A still-running job should NOT be resumable - doing so starts a
        second pipeline against the same SAP Proposal (double release)."""
        job_id = self._seed(mongo_db, "running")
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": "QA"}, timeout=TIMEOUT)
        if r.status_code == 200:
            new_id = r.json().get("job_id")
            if new_id:
                self.JOBS.append(new_id)
                client.post(f"{API}/production-confirmation/create-and-release-order/{new_id}/cancel",
                            timeout=TIMEOUT)
        assert r.status_code == 400, (
            f"resume accepted a status='running' job (HTTP {r.status_code}) - duplicate "
            "pipeline against the same SAP Proposal is possible")

    def test_resume_on_done_job(self, client, mongo_db):
        job_id = self._seed(mongo_db, "done")
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": "QA"}, timeout=TIMEOUT)
        if r.status_code == 200:
            new_id = r.json().get("job_id")
            if new_id:
                self.JOBS.append(new_id)
                client.post(f"{API}/production-confirmation/create-and-release-order/{new_id}/cancel",
                            timeout=TIMEOUT)
        assert r.status_code == 400, (
            f"resume accepted an already-completed job (HTTP {r.status_code})")
