"""Store Approval JOURNAL endpoint (Aug 2026 addition).

Covers GET /api/store-requests/journal (public, all statuses) vs
GET /api/store-requests (public queue, pending/partial only), plus the
legacy {site, qty} location shape being preserved in the API payload so
the frontend fallback can render it.

Seeds synthetic store_requests docs tagged TEST_JOURNAL_* and removes
them in teardown. Real production docs are never touched.
"""
import os
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
API = f"{base_url.rstrip('/')}/api"

MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
COLL = "store_requests"
TAG = "TEST_JOURNAL"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


def _doc(status, material, site, requester, minutes_ago, locations, comp_id="TEST_JOURNAL_COMP_1", comp_desc="Test Journal Component Alpha"):
    now = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "_id": f"{TAG}_{uuid.uuid4()}",
        "job_id": f"{TAG}_job_{uuid.uuid4()}",
        "production_proposal_id": f"{TAG}_PROP_{status}",
        "material_id": material,
        "site_id": site,
        "quantity": 10.0,
        "unit_code": "EA",
        "requester": requester,
        "components": [{
            "product_id": comp_id,
            "description": comp_desc,
            "unit_of_measure": "KG",
            "required_qty": 5.0,
            "available_qty": 2.0,
            "locations": locations,
            "issued_qty": 2.0 if status in ("resolved", "cancelled") else None,
            "shortfall": 3.0 if status in ("resolved", "cancelled") else None,
        }],
        "status": status,
        "store_actor": "TEST_STORE_USER" if status != "pending" else None,
        "store_decision": "send_to_planner" if status in ("partial_pending_planner", "resolved", "cancelled") else None,
        "planner_actor": "TEST_PLANNER" if status in ("resolved", "cancelled") else None,
        "planner_decision": "approve" if status == "resolved" else ("reject" if status == "cancelled" else None),
        "resolution": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }


NEW_SHAPE = [{"warehouse": "TEST_WH_MAIN", "stock_status": "Unrestricted", "qty": 2.0}]
LEGACY_SHAPE = [{"site": "IN-P2", "qty": 2.0}]


@pytest.fixture(scope="module")
def seeded(db):
    docs = [
        _doc("pending", "TEST_JOURNAL_MAT_PEND", "TEST_S1", "TEST_Requester_Alpha", 5, NEW_SHAPE),
        _doc("partial_pending_planner", "TEST_JOURNAL_MAT_PARTIAL", "TEST_S1", "TEST_Requester_Beta", 15, NEW_SHAPE),
        _doc("resolved", "TEST_JOURNAL_MAT_RESOLVED", "TEST_S2", "TEST_Requester_Gamma", 25, NEW_SHAPE),
        _doc("cancelled", "TEST_JOURNAL_MAT_CANCELLED", "TEST_S2", "TEST_Requester_Delta", 35, LEGACY_SHAPE),
    ]
    db[COLL].insert_many(docs)
    yield {d["status"]: d for d in docs}
    db[COLL].delete_many({"_id": {"$regex": f"^{TAG}_"}})


class TestJournalEndpoint:
    def test_journal_returns_all_statuses(self, seeded):
        r = requests.get(f"{API}/store-requests/journal", timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "requests" in body and isinstance(body["requests"], list)
        ids = {x["_id"] for x in body["requests"]}
        for status, doc in seeded.items():
            assert doc["_id"] in ids, f"journal missing seeded {status} request"

    def test_journal_no_mongo_objectid_leak(self, seeded):
        r = requests.get(f"{API}/store-requests/journal", timeout=60)
        for x in r.json()["requests"]:
            assert not isinstance(x.get("_id"), dict)
            assert isinstance(x["_id"], str)

    def test_queue_excludes_resolved_and_cancelled(self, seeded):
        r = requests.get(f"{API}/store-requests", timeout=60)
        assert r.status_code == 200, r.text
        reqs = r.json()["requests"]
        ids = {x["_id"] for x in reqs}
        assert seeded["pending"]["_id"] in ids
        assert seeded["partial_pending_planner"]["_id"] in ids
        assert seeded["resolved"]["_id"] not in ids
        assert seeded["cancelled"]["_id"] not in ids
        assert {x["status"] for x in reqs} <= {"pending", "partial_pending_planner"}

    def test_journal_sorted_desc_by_created_at(self, seeded):
        reqs = requests.get(f"{API}/store-requests/journal", timeout=60).json()["requests"]
        seeded_ids = {d["_id"] for d in seeded.values()}
        order = [x["material_id"] for x in reqs if x["_id"] in seeded_ids]
        assert order == [
            "TEST_JOURNAL_MAT_PEND",
            "TEST_JOURNAL_MAT_PARTIAL",
            "TEST_JOURNAL_MAT_RESOLVED",
            "TEST_JOURNAL_MAT_CANCELLED",
        ], order

    def test_journal_route_not_swallowed_by_request_id_route(self, seeded):
        """/store-requests/journal must not 404 as an unknown request_id."""
        r = requests.get(f"{API}/store-requests/journal", timeout=60)
        assert r.status_code == 200
        assert "requests" in r.json()
        bogus = requests.get(f"{API}/store-requests/{TAG}_does_not_exist", timeout=60)
        assert bogus.status_code == 404

    def test_journal_preserves_legacy_and_new_location_shapes(self, seeded):
        reqs = {x["_id"]: x for x in requests.get(f"{API}/store-requests/journal", timeout=60).json()["requests"]}
        legacy = reqs[seeded["cancelled"]["_id"]]["components"][0]["locations"][0]
        assert legacy.get("site") == "IN-P2"
        assert "warehouse" not in legacy
        new = reqs[seeded["resolved"]["_id"]]["components"][0]["locations"][0]
        assert new.get("warehouse") == "TEST_WH_MAIN"
        assert new.get("stock_status") == "Unrestricted"

    def test_detail_fetch_of_resolved_request(self, seeded):
        rid = seeded["resolved"]["_id"]
        r = requests.get(f"{API}/store-requests/{rid}", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "resolved"
        assert data["components"][0]["issued_qty"] == 2.0

    def test_issue_on_non_pending_request_rejected(self, seeded):
        rid = seeded["resolved"]["_id"]
        r = requests.post(f"{API}/store-requests/{rid}/issue", json={
            "issued": [{"product_id": "TEST_JOURNAL_COMP_1", "issued_qty": 5}],
            "decision": None, "actor": "TEST_STORE_USER",
        }, timeout=60)
        assert r.status_code == 400, r.text
        assert "no longer pending" in r.json()["detail"].lower()

    def test_real_production_docs_untouched(self, db, seeded):
        real = db[COLL].count_documents({"_id": {"$not": {"$regex": f"^{TAG}_"}}, "requester": "Ankit"})
        assert real >= 3, f"expected the 3 real Ankit requests to still exist, found {real}"
