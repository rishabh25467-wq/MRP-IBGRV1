"""Iteration 163 - duplicate Store Request fix (Sep 12 2026)

Verifies that a retry of "Create Production Order" for the exact same
material/site/quantity/requester DOES NOT create a second SAP Production
Proposal + second Store Request while an earlier one is still open.

Two layers:
  - Unit: store_approval_service.find_open_request across every dimension.
  - Integration: _run_create_and_release_job with check_component_availability
    monkey-patched to force a short_rm result AND _create_proposal_for_payload
    monkey-patched to assert it is NEVER called on the duplicate path.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

import store_approval_service  # noqa: E402
import job_store  # noqa: E402
import server  # noqa: E402  (imports FastAPI app, but we just call the internal coroutine)


MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture
def cleanup(db):
    created_request_ids = []
    created_job_ids = []
    yield {"requests": created_request_ids, "jobs": created_job_ids}
    if created_request_ids:
        db["store_requests"].delete_many({"_id": {"$in": created_request_ids}})
    if created_job_ids:
        db["job_store"].delete_many({"_id": {"$in": created_job_ids}})


def _seed_open_request(db, request_id, site_id, material_id, quantity, requester, status="pending"):
    doc = {
        "_id": request_id,
        "job_id": f"TESTJOB_{request_id}",
        "production_proposal_id": "TESTPROP_1",
        "material_id": material_id, "site_id": site_id, "quantity": quantity, "unit_code": "EA",
        "requester": requester,
        "components": [], "status": status,
        "store_actor": None, "store_decision": None, "planner_actor": None, "planner_decision": None,
        "resolution": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "resolved_at": None,
    }
    db["store_requests"].insert_one(doc)
    return doc


# -----------------------------------------------------------------------
# Unit: find_open_request across every dimension.
# -----------------------------------------------------------------------
class TestFindOpenRequest:
    def test_exact_match_returns_doc(self, db, cleanup):
        rid = "TEST_ITER163_A"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com")
        result = store_approval_service.find_open_request(db, "P1", "MAT-A", 10.0, "tester@x.com")
        assert result is not None
        assert result["_id"] == rid

    def test_different_requester_returns_none(self, db, cleanup):
        rid = "TEST_ITER163_B"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com")
        assert store_approval_service.find_open_request(db, "P1", "MAT-A", 10.0, "someone.else@x.com") is None

    def test_different_quantity_returns_none(self, db, cleanup):
        rid = "TEST_ITER163_C"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com")
        assert store_approval_service.find_open_request(db, "P1", "MAT-A", 11.0, "tester@x.com") is None

    def test_different_site_returns_none(self, db, cleanup):
        rid = "TEST_ITER163_D"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com")
        assert store_approval_service.find_open_request(db, "P2", "MAT-A", 10.0, "tester@x.com") is None

    def test_different_material_returns_none(self, db, cleanup):
        rid = "TEST_ITER163_E"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com")
        assert store_approval_service.find_open_request(db, "P1", "MAT-B", 10.0, "tester@x.com") is None

    def test_resolved_status_returns_none(self, db, cleanup):
        rid = "TEST_ITER163_F"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com", status="resolved")
        assert store_approval_service.find_open_request(db, "P1", "MAT-A", 10.0, "tester@x.com") is None

    def test_cancelled_status_returns_none(self, db, cleanup):
        rid = "TEST_ITER163_G"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com", status="cancelled")
        assert store_approval_service.find_open_request(db, "P1", "MAT-A", 10.0, "tester@x.com") is None

    def test_issuing_status_counts_as_open(self, db, cleanup):
        rid = "TEST_ITER163_H"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com", status="issuing")
        result = store_approval_service.find_open_request(db, "P1", "MAT-A", 10.0, "tester@x.com")
        assert result is not None and result["_id"] == rid

    def test_partial_pending_planner_counts_as_open(self, db, cleanup):
        rid = "TEST_ITER163_I"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-A", 10.0, "tester@x.com", status="partial_pending_planner")
        result = store_approval_service.find_open_request(db, "P1", "MAT-A", 10.0, "tester@x.com")
        assert result is not None and result["_id"] == rid


# -----------------------------------------------------------------------
# Integration: _run_create_and_release_job on the duplicate path.
# check_component_availability is stubbed to force short_rm; the real
# SAP proposal-create is stubbed and asserted to NEVER be called on the
# duplicate path. Also verifies the non-duplicate path DOES call it.
# -----------------------------------------------------------------------
class TestRunCreateAndReleaseJobDedup:
    def _payload(self, actor="tester@x.com", material="MAT-DEDUP-A", site="P1", qty=5.0):
        return server.CreateProductionProposalRequest(
            material_id=material, site_id=site, quantity=qty, unit_code="EA", actor=actor,
        )

    def _make_availability(self, material_id, short=True):
        # Shape mirrors production_confirmation_service.check_component_availability output.
        return {
            "checked": True,
            "components": [
                {"product_id": "COMP-1", "description": "d", "unit_of_measure": "EA",
                 "required_qty": 5.0, "available_qty": 0.0 if short else 100.0,
                 "sufficient": not short, "is_sub_assembly": False,
                 "locations": []},
            ],
        }

    def _stub(self, monkeypatch, short=True, proposal_called_tracker=None):
        monkeypatch.setattr(
            server.production_confirmation_service, "check_component_availability",
            lambda *a, **k: self._make_availability("x", short=short),
        )

        async def fake_create_proposal(payload, avail_dt, jid):
            if proposal_called_tracker is not None:
                proposal_called_tracker.append(jid)
            return "PROP_" + jid[:8]

        monkeypatch.setattr(server, "_create_proposal_for_payload", fake_create_proposal)

        # Avoid log_proposal_creation actually running SAP-side history writes:
        monkeypatch.setattr(server.production_confirmation_service, "log_proposal_creation",
                            lambda *a, **k: None)

        async def fake_continue(jid, payload, proposal_id, is_resume=False):
            job_store.update_job(server.db, jid, {"status": "done", "production_proposal_id": proposal_id})

        monkeypatch.setattr(server, "_continue_order_creation", fake_continue)

    def test_duplicate_blocks_before_proposal(self, db, cleanup, monkeypatch):
        # Seed an open request that will collide.
        rid = "TEST_ITER163_DUP"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-DEDUP-A", 5.0, "tester@x.com")

        proposal_calls = []
        self._stub(monkeypatch, short=True, proposal_called_tracker=proposal_calls)

        # Manually create the job doc.
        job_id = "TEST_JOB_DUP_" + uuid.uuid4().hex[:8]
        cleanup["jobs"].append(job_id)
        job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None,
                                          "payload_snapshot": self._payload().dict(), "actor_user_id": None})

        payload = self._payload()
        asyncio.get_event_loop().run_until_complete(
            server._run_create_and_release_job(job_id, payload, None)
        )

        job = job_store.get_job(db, job_id)
        assert job["status"] == "failed", f"expected failed, got {job['status']} err={job.get('error')}"
        assert job["result"]["reason"] == "duplicate_store_request"
        assert job["result"]["production_proposal_id"] is None
        assert rid in (job["error"] or ""), f"Existing request id not surfaced in error: {job['error']}"
        assert proposal_calls == [], "SAP _create_proposal_for_payload MUST NOT be called on duplicate path"

    def test_different_requester_not_blocked(self, db, cleanup, monkeypatch):
        rid = "TEST_ITER163_DUP2"
        cleanup["requests"].append(rid)
        _seed_open_request(db, rid, "P1", "MAT-DEDUP-A", 5.0, "tester@x.com")

        proposal_calls = []
        self._stub(monkeypatch, short=True, proposal_called_tracker=proposal_calls)

        # Patch create_request to no-op so we don't need real short_components shape/writes.
        monkeypatch.setattr(server.store_approval_service, "create_request",
                            lambda *a, **k: {"_id": "TEST_SR_STUB"})

        job_id = "TEST_JOB_DIFFREQ_" + uuid.uuid4().hex[:8]
        cleanup["jobs"].append(job_id)
        payload = self._payload(actor="different.user@x.com")
        job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None,
                                          "payload_snapshot": payload.dict(), "actor_user_id": None})

        asyncio.get_event_loop().run_until_complete(
            server._run_create_and_release_job(job_id, payload, None)
        )

        job = job_store.get_job(db, job_id)
        # A DIFFERENT requester must NOT be blocked - it should have called _create_proposal_for_payload.
        assert len(proposal_calls) == 1, f"different-requester path should call proposal-create once, got {proposal_calls}"
        assert job["status"] == "waiting_store_approval", f"expected waiting_store_approval, got {job['status']} err={job.get('error')}"

    def test_no_shortage_no_dedup_check(self, db, cleanup, monkeypatch):
        # Regression: normal (non-short) flow still calls proposal-create.
        proposal_calls = []
        self._stub(monkeypatch, short=False, proposal_called_tracker=proposal_calls)

        job_id = "TEST_JOB_NOSHORT_" + uuid.uuid4().hex[:8]
        cleanup["jobs"].append(job_id)
        payload = self._payload(material="MAT-NOSHORT")
        job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None,
                                          "payload_snapshot": payload.dict(), "actor_user_id": None})

        asyncio.get_event_loop().run_until_complete(
            server._run_create_and_release_job(job_id, payload, None)
        )

        assert len(proposal_calls) == 1
        job = job_store.get_job(db, job_id)
        assert job["status"] == "done"
