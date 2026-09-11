"""Iteration 155 - verifies the Sep 11 2026 rule reversal:
- Partial issue where SAP ACCEPTS the movement -> status='resolved',
  resolution='store_proceeded_partial' (never reopenable).
- Partial issue where SAP REJECTS the movement -> status='resolved_balance_pending',
  stays reopenable (regression safety).
- Full issue -> status='resolved', resolution='full_issue' (unchanged).
- Planner approve on partial_pending_planner -> status='resolved',
  resolution='planner_approved_partial' (no longer resolved_balance_pending).
- Reopen round that fully clears the shortfall -> resolution='balance_completed'.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")

import store_approval_service as svc  # noqa: E402


@pytest.fixture(scope="module")
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


class FakeSapClient:
    def __init__(self, ok=True, error_message="Negative stock not permitted"):
        self.ok = ok
        self.error_message = error_message
        self.calls = []

    def goods_movement(self, **kwargs):
        self.calls.append(kwargs)
        if self.ok:
            return {"ok": True, "raw_xml": ""}
        # Simulate SAP-rejection shape: ok=False + error_detail from clarifier
        return {"ok": False, "error_detail": self.error_message, "raw_xml": ""}


def _seed_request(db, site_id="P1", required=10.0, available_loc_qty=100.0, product_id=None):
    """Insert a fresh short-component request bypassing create_request so we
    can control the shape precisely (skip counter side effects)."""
    product_id = product_id or f"TEST_PROD_{uuid.uuid4().hex[:6]}"
    rid = f"TEST-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    rm_wh = f"{site_id}/{site_id}-RM"
    doc = {
        "_id": rid,
        "job_id": f"job-{rid}",
        "production_proposal_id": "PP-TEST",
        "material_id": "MAT-TEST",
        "site_id": site_id,
        "quantity": 1,
        "unit_code": "EA",
        "requester": "test",
        "components": [{
            "product_id": product_id,
            "description": "Test",
            "unit_of_measure": "EA",
            "required_qty": required,
            "available_qty": available_loc_qty,
            "locations": [{
                "warehouse_id": rm_wh,
                "qty": available_loc_qty,
                "owner": "RI",
                "stock_status": "Not Assigned",
                "restricted": False,
            }],
            "issued_qty": None,
            "shortfall": None,
        }],
        "status": "pending",
        "store_actor": None,
        "store_decision": None,
        "planner_actor": None,
        "planner_decision": None,
        "resolution": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }
    db[svc.COLLECTION].insert_one(doc)
    return rid, product_id


@pytest.fixture(autouse=True)
def _force_dry_run_false(monkeypatch):
    # We want the code path through _trigger_goods_movement to actually call
    # our FakeSapClient; is_dry_run() controls only whether the SAP client's
    # own dry_run flag is set - our fake ignores it, so keep it simple.
    monkeypatch.setenv("SAP_GOODS_MOVEMENT_DRY_RUN", "true")
    yield


@pytest.fixture
def cleanup(db):
    created = []
    yield created
    if created:
        db[svc.COLLECTION].delete_many({"_id": {"$in": created}})


# ---------- 1. Partial issue, SAP ACCEPTS -> closes as 'resolved' ----------
def test_partial_issue_sap_accepts_closes_for_good(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    sap = FakeSapClient(ok=True)

    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 4.0}], decision="proceed", store_actor="tester")
    final = svc.run_issue_movements(db, rid, sap)

    assert final["status"] == "resolved", f"expected resolved, got {final['status']}"
    assert final["resolution"] == "store_proceeded_partial"
    assert final["resolved_at"] is not None
    assert final["components"][0]["shortfall"] == 6.0
    assert final["components"][0]["goods_movement"]["ok"] is True


# ---------- 2. Partial issue, SAP REJECTS -> stays 'resolved_balance_pending' ----------
def test_partial_issue_sap_rejects_stays_reopenable(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    sap = FakeSapClient(ok=False, error_message="Negative stock not permitted")

    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 4.0}], decision="proceed", store_actor="tester")
    final = svc.run_issue_movements(db, rid, sap)

    assert final["status"] == "resolved_balance_pending", f"expected resolved_balance_pending, got {final['status']}"
    assert final["resolution"] == "store_proceeded_partial"
    assert final["components"][0]["goods_movement"]["ok"] is False


# ---------- 3. Full issue -> resolved / full_issue ----------
def test_full_issue_unchanged(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    sap = FakeSapClient(ok=True)

    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 10.0}], decision="proceed", store_actor="tester")
    final = svc.run_issue_movements(db, rid, sap)

    assert final["status"] == "resolved"
    assert final["resolution"] == "full_issue"


# ---------- 4. send_to_planner then planner approves -> resolved / planner_approved_partial ----------
def test_planner_approve_partial_closes_for_good(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    sap = FakeSapClient(ok=True)

    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 4.0}], decision="send_to_planner", store_actor="tester")
    mid = svc.run_issue_movements(db, rid, sap)
    assert mid["status"] == "partial_pending_planner", f"pre-planner status: {mid['status']}"

    final = svc.planner_decision(db, rid, decision="approve", planner_actor="planner1")
    assert final["status"] == "resolved", f"expected resolved after planner approve, got {final['status']}"
    assert final["resolution"] == "planner_approved_partial"
    assert final["resolved_at"] is not None


# ---------- 5. Planner reject still cancels ----------
def test_planner_reject_cancels(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    sap = FakeSapClient(ok=True)

    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 4.0}], decision="send_to_planner", store_actor="tester")
    svc.run_issue_movements(db, rid, sap)
    final = svc.planner_decision(db, rid, decision="reject", planner_actor="planner1")
    assert final["status"] == "cancelled"


# ---------- 6. Reopen round (from a genuine SAP-reject state) that fully clears -> balance_completed ----------
def test_reopen_round_balance_completed(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    # Round 1: SAP rejects -> resolved_balance_pending
    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 4.0}], decision="proceed", store_actor="tester")
    r1 = svc.run_issue_movements(db, rid, FakeSapClient(ok=False))
    assert r1["status"] == "resolved_balance_pending"

    # Round 2: reopen, issue the remaining 6, SAP accepts -> balance_completed
    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 6.0}], decision="proceed", store_actor="tester")
    r2 = svc.run_issue_movements(db, rid, FakeSapClient(ok=True))
    assert r2["status"] == "resolved"
    assert r2["resolution"] == "balance_completed"


# ---------- 7. Reopen round that is still partial+SAP-accepts -> also closes as balance_completed ----------
def test_reopen_round_partial_sap_accepts_closes(db, cleanup):
    rid, pid = _seed_request(db, required=10.0)
    cleanup.append(rid)
    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 4.0}], decision="proceed", store_actor="tester")
    svc.run_issue_movements(db, rid, FakeSapClient(ok=False))
    # Reopen but only issue 2 more (still short by 4) - per new rule this closes for good.
    svc.start_issue(db, rid, [{"product_id": pid, "issued_qty": 2.0}], decision="proceed", store_actor="tester")
    r2 = svc.run_issue_movements(db, rid, FakeSapClient(ok=True))
    assert r2["status"] == "resolved", f"expected resolved on partial reopen with SAP accept, got {r2['status']}"
    assert r2["resolution"] == "balance_completed"
