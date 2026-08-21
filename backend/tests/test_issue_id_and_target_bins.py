"""Aug 2026 additions to the Store Approval flow:

1. store_approval_service.submit_issue() now stamps a separate `issue_id`
   ("{site_id}-I000123") from its OWN counter key "{site_id}:issue".
2. GET /api/store-requests/target-bins?site_id=X -> distinct non-null
   target_logistics_area_id values previously used at that site.

All docs seeded here are tagged TEST_ISSUEID_* and removed in teardown,
including the two counter docs for the synthetic site.
"""
import os
import re
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
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
API = f"{base_url.rstrip('/')}/api"

MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
COLL = "store_requests"
COUNTERS = "store_request_counters"
TAG = "TEST_ISSUEID"
SITE = "TESTISSUEID1"  # synthetic site, never used by real data


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def tracker():
    return {"requests": [], "jobs": []}


@pytest.fixture(scope="module", autouse=True)
def cleanup(db, tracker):
    yield
    db[COLL].delete_many({"_id": {"$in": tracker["requests"]}})
    db["background_jobs"].delete_many({"_id": {"$in": tracker["jobs"]}})
    db[COUNTERS].delete_many({"_id": {"$in": [SITE, f"{SITE}:issue"]}})


def seed_pending(db, tracker, site=SITE, target_bin=None, status="pending"):
    """Pending request with a deliberate shortfall (required 10, we will
    issue 4) so the 'send_to_planner' branch is used - this avoids kicking
    off the real SAP Proposal->Order resume pipeline. Components carry no
    `locations`, so no Goods Movement call can fire either."""
    job_id = f"{TAG}_job_{uuid.uuid4()}"
    req_id = f"{TAG}_{uuid.uuid4()}"
    now = datetime.now(timezone.utc)
    db["background_jobs"].insert_one({
        "_id": job_id, "status": "waiting_store_approval", "result": None, "error": None,
        "production_proposal_id": f"{TAG}_PROP", "store_request_id": req_id,
        "created_at": now, "updated_at": now,
    })
    doc = {
        "_id": req_id, "job_id": job_id, "production_proposal_id": f"{TAG}_PROP",
        "material_id": f"{TAG}_MAT", "site_id": site, "quantity": 1.0, "unit_code": "EA",
        "requester": f"{TAG}_Requester",
        "components": [{
            "product_id": f"{TAG}_COMP_A", "description": f"{TAG} comp A", "unit_of_measure": "KG",
            "required_qty": 10.0, "available_qty": 4.0, "locations": [],
            "issued_qty": None, "shortfall": None,
        }],
        "status": status, "store_actor": None, "store_decision": None,
        "planner_actor": None, "planner_decision": None, "resolution": None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    }
    if target_bin is not None:
        doc["target_logistics_area_id"] = target_bin
    db[COLL].insert_one(doc)
    tracker["requests"].append(req_id)
    tracker["jobs"].append(job_id)
    return req_id


def issue(req_id, bin_id="TESTBIN-A", actor=f"{TAG}_Issuer"):
    return requests.post(f"{API}/store-requests/{req_id}/issue", json={
        "issued": [{"product_id": f"{TAG}_COMP_A", "issued_qty": 4.0}],
        "decision": "send_to_planner", "actor": actor,
        "target_logistics_area_id": bin_id,
    }, timeout=60)


ISSUE_ID_RE = re.compile(rf"^{SITE}-I\d{{6}}$")


# ---------------------------------------------------------------- Issue ID
class TestIssueIdGeneration:
    def test_issue_id_created_and_shaped_correctly(self, db, tracker):
        req_id = seed_pending(db, tracker)
        r = issue(req_id)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["issue_id"], "no issue_id on the issue response"
        assert ISSUE_ID_RE.match(body["issue_id"]), f"bad issue_id shape: {body['issue_id']}"
        assert body["issue_id"] != body["_id"]
        assert body["status"] == "partial_pending_planner"
        assert body["target_logistics_area_id"] == "TESTBIN-A"

        # GET back to confirm persistence
        g = requests.get(f"{API}/store-requests/{req_id}", timeout=60)
        assert g.status_code == 200, g.text
        assert g.json()["issue_id"] == body["issue_id"]
        assert isinstance(g.json()["issue_id"], str)

    def test_issue_id_counter_is_independent_of_request_id_counter(self, db, tracker):
        """Two issues in a row must be strictly sequential on the ':issue'
        counter, and creating request IDs in between must not bump it."""
        first = issue(seed_pending(db, tracker)).json()["issue_id"]
        # burn a Request-ID sequence number for the same site
        req_counter_before = db[COUNTERS].find_one({"_id": SITE})
        db[COUNTERS].find_one_and_update({"_id": SITE}, {"$inc": {"seq": 1}}, upsert=True)
        second = issue(seed_pending(db, tracker)).json()["issue_id"]

        n1 = int(first.split("-I")[1])
        n2 = int(second.split("-I")[1])
        assert n2 == n1 + 1, f"issue sequence skipped/collided: {first} -> {second}"

        issue_counter = db[COUNTERS].find_one({"_id": f"{SITE}:issue"})
        assert issue_counter is not None and issue_counter["seq"] == n2
        # the request counter doc is a genuinely separate document
        assert (req_counter_before or {}).get("_id") != f"{SITE}:issue"

    def test_issue_ids_unique_across_requests(self, db, tracker):
        ids = {issue(seed_pending(db, tracker)).json()["issue_id"] for _ in range(3)}
        assert len(ids) == 3, f"duplicate issue_ids generated: {ids}"

    def test_second_issue_on_same_request_rejected_and_issue_id_unchanged(self, db, tracker):
        req_id = seed_pending(db, tracker)
        first = issue(req_id).json()["issue_id"]
        again = issue(req_id)
        assert again.status_code == 400, again.text
        assert "no longer pending" in again.json()["detail"]
        assert requests.get(f"{API}/store-requests/{req_id}", timeout=60).json()["issue_id"] == first

    def test_pending_request_has_no_issue_id(self, db, tracker):
        req_id = seed_pending(db, tracker)
        body = requests.get(f"{API}/store-requests/{req_id}", timeout=60).json()
        assert body.get("issue_id") is None

    def test_issue_id_present_in_journal_payload(self, db, tracker):
        req_id = seed_pending(db, tracker)
        expected = issue(req_id).json()["issue_id"]
        rows = requests.get(f"{API}/store-requests/journal", timeout=60).json()["requests"]
        row = next((x for x in rows if x["_id"] == req_id), None)
        assert row is not None, "issued request missing from journal"
        assert row["issue_id"] == expected
        assert row["components"][0]["issued_qty"] == 4.0


# ------------------------------------------------------------- Target bins
class TestTargetBinsEndpoint:
    def test_unknown_site_returns_empty_list(self):
        r = requests.get(f"{API}/store-requests/target-bins",
                         params={"site_id": f"{TAG}_NEVER_USED_SITE"}, timeout=60)
        assert r.status_code == 200, r.text
        assert r.json() == {"bins": []}

    def test_missing_site_id_is_422(self):
        r = requests.get(f"{API}/store-requests/target-bins", timeout=60)
        assert r.status_code == 422, r.text

    def test_returns_distinct_sorted_non_null_bins_for_site(self, db, tracker):
        seed_pending(db, tracker, target_bin="ZBIN-2", status="resolved")
        seed_pending(db, tracker, target_bin="ZBIN-1", status="resolved")
        seed_pending(db, tracker, target_bin="ZBIN-1", status="resolved")  # duplicate
        seed_pending(db, tracker, target_bin=None, status="resolved")      # missing field
        r = requests.get(f"{API}/store-requests/target-bins", params={"site_id": SITE}, timeout=60)
        assert r.status_code == 200, r.text
        bins = r.json()["bins"]
        assert bins == sorted(bins), "bins not sorted"
        assert len([b for b in bins if b == "ZBIN-1"]) == 1, "duplicates not collapsed"
        assert {"ZBIN-1", "ZBIN-2"} <= set(bins)
        assert None not in bins and "" not in bins

    def test_bin_typed_at_issue_time_becomes_available_afterwards(self, db, tracker):
        new_bin = "ZBIN-FRESH-ORGANIC"
        before = requests.get(f"{API}/store-requests/target-bins", params={"site_id": SITE}, timeout=60).json()["bins"]
        assert new_bin not in before
        issue(seed_pending(db, tracker), bin_id=new_bin)
        after = requests.get(f"{API}/store-requests/target-bins", params={"site_id": SITE}, timeout=60).json()["bins"]
        assert new_bin in after, "newly typed bin did not grow the per-site list"

    def test_site_scoped(self, db, tracker):
        other_site_bins = requests.get(f"{API}/store-requests/target-bins",
                                       params={"site_id": "TESTISSUEID2"}, timeout=60).json()["bins"]
        assert "ZBIN-1" not in other_site_bins, "bins leaking across sites"


# --------------------------------------------------------------- Regression
class TestRegressionSmoke:
    def test_queue_and_journal_still_ok(self):
        q = requests.get(f"{API}/store-requests", timeout=60)
        j = requests.get(f"{API}/store-requests/journal", timeout=60)
        assert q.status_code == 200 and j.status_code == 200
        assert {x["status"] for x in q.json()["requests"]} <= {"pending", "partial_pending_planner"}
        assert len(j.json()["requests"]) >= len(q.json()["requests"])

    def test_issue_requires_actor(self, db, tracker):
        req_id = seed_pending(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": f"{TAG}_COMP_A", "issued_qty": 4.0}],
            "decision": "send_to_planner", "actor": "  ",
            "target_logistics_area_id": "TESTBIN-A",
        }, timeout=60)
        assert r.status_code == 400, r.text

    def test_unknown_request_id_404(self):
        r = requests.get(f"{API}/store-requests/{TAG}_does_not_exist", timeout=60)
        assert r.status_code == 404
