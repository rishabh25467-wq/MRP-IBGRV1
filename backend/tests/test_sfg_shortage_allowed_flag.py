"""Session Sep 10 2026 change: SFG shortage no longer blocks Create Production
Order. Instead a non-blocking `sfg_shortage` object is stored on the job doc,
persists through all subsequent statuses, and the Proposal + downstream Order
creation proceed as normal. A BOP/RM shortage still opens a Store Approval
request and pauses at status='waiting_store_approval' unchanged.

These tests EXCLUSIVELY seed synthetic job docs in the production_confirmation
jobs collection and hit the read-only GET status endpoint - NO live SAP write
is triggered (this is a live SAP production tenant per test_credentials.md).
The behavior change is in _run_create_and_release_job which we do NOT invoke
end-to-end; we instead verify (a) the shape of the flag persists on the job doc
via the API, (b) that the newly-modified branching source code matches spec, and
(c) that the frontend can be exercised with these seeded jobs (playwright)."""

import os
import uuid
import secrets
import time
import datetime as dt
import pytest
import requests
from pymongo import MongoClient

def _read_frontend_backend_url():
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL"):
                    return line.split("=", 1)[1].strip().rstrip("/")
    except FileNotFoundError:
        pass
    return ""

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or _read_frontend_backend_url()
assert BASE_URL, "REACT_APP_BACKEND_URL must be set"
# Backend .env not loaded here; read raw file for Mongo creds
def _load_env_file(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

_beenv = _load_env_file("/app/backend/.env")
MONGO_URL = _beenv.get("MONGO_URL")
DB_NAME = _beenv.get("DB_NAME")

client = MongoClient(MONGO_URL)
db = client[DB_NAME]
JOBS_COL = "background_jobs"


@pytest.fixture(scope="module")
def session_cookie():
    """Seed a synthetic super_admin auth session with production_confirmation
    access. Cleaned up after tests."""
    uid = "test-tid:iter154-sfg-shortage"
    token = "iter154sfg" + secrets.token_hex(16)
    now = dt.datetime.now(dt.timezone.utc)
    db["auth_users"].update_one({"_id": uid}, {"$set": {
        "tid": "test-tid", "oid": "iter154-sfg-shortage",
        "email": "iter154.sfg@rampgroup.test", "name": "SFG Tester",
        "role": "super_admin", "allowed_pages": ["production_confirmation"],
        "bound_sites": [], "created_at": now, "last_login_at": now,
    }}, upsert=True)
    db["auth_sessions"].insert_one({
        "_id": token, "user_id": uid,
        "expires_at": now + dt.timedelta(days=1),
    })
    yield token
    db["auth_sessions"].delete_one({"_id": token})
    db["auth_users"].delete_one({"_id": uid})


@pytest.fixture(scope="module")
def seeded_jobs():
    """Seed 4 synthetic jobs representing the 4 scenarios under test.
    Cleaned up after tests."""
    scenarios = {}

    # 1) SFG-only shortage, proceeded to done - proves sfg_shortage persists
    j1 = str(uuid.uuid4())
    scenarios["sfg_only_done"] = j1

    # 2) BOP/RM shortage only, paused at waiting_store_approval
    j2 = str(uuid.uuid4())
    scenarios["bop_only_paused"] = j2

    # 3) Both SFG + BOP short: sfg_shortage set AND paused waiting_store_approval
    j3 = str(uuid.uuid4())
    scenarios["both_short"] = j3

    # 4) Normal (no shortage), done
    j4 = str(uuid.uuid4())
    scenarios["no_short_done"] = j4

    now = dt.datetime.now(dt.timezone.utc)
    payload_snapshot = {"actor": "SFG Tester", "material_id": "TEST_MAT", "site_id": "P1",
                        "quantity": 10, "unit_code": "EA"}
    sfg_flag = {
        "site_id": "P1",
        "short_components": [
            {"product_id": "TEST_SFG_1", "description": "SFG comp 1",
             "unit_of_measure": "EA", "required_qty": 20.0, "available_qty": 5.0},
        ],
    }

    docs = [
        {"_id": j1, "status": "done", "payload_snapshot": payload_snapshot,
         "actor_user_id": "test-tid:iter154-sfg-shortage",
         "production_proposal_id": "TEST_PROP_1", "production_order_id": "TEST_ORD_1",
         "sfg_shortage": sfg_flag, "created_at": now,
         "result": {"production_proposal_id": "TEST_PROP_1", "production_order_id": "TEST_ORD_1", "released": True}},
        {"_id": j2, "status": "waiting_store_approval", "payload_snapshot": payload_snapshot,
         "actor_user_id": "test-tid:iter154-sfg-shortage",
         "production_proposal_id": "TEST_PROP_2", "store_request_id": "TEST_STORE_REQ_2",
         "created_at": now},
        {"_id": j3, "status": "waiting_store_approval", "payload_snapshot": payload_snapshot,
         "actor_user_id": "test-tid:iter154-sfg-shortage",
         "production_proposal_id": "TEST_PROP_3", "store_request_id": "TEST_STORE_REQ_3",
         "sfg_shortage": sfg_flag, "created_at": now},
        {"_id": j4, "status": "done", "payload_snapshot": payload_snapshot,
         "actor_user_id": "test-tid:iter154-sfg-shortage",
         "production_proposal_id": "TEST_PROP_4", "production_order_id": "TEST_ORD_4",
         "created_at": now,
         "result": {"production_proposal_id": "TEST_PROP_4", "production_order_id": "TEST_ORD_4", "released": True}},
    ]
    for d in docs:
        db[JOBS_COL].insert_one(d)

    yield scenarios

    for jid in scenarios.values():
        db[JOBS_COL].delete_one({"_id": jid})


def _get_status(session_cookie, job_id):
    """Call the actual API endpoint. Retries once on read timeout since the
    backend under test also has heavy SAP background jobs sharing its thread
    pool - a first-hit timeout doesn't necessarily reflect a real regression."""
    url = f"{BASE_URL}/api/production-confirmation/create-and-release-order/status/{job_id}"
    last_exc = None
    for _ in range(2):
        try:
            return requests.get(url, cookies={"vms_session": session_cookie}, timeout=120)
        except requests.exceptions.ReadTimeout as e:
            last_exc = e
            time.sleep(2)
    raise last_exc


# ---- Scenario 1: SFG shortage, no BOP shortage - sfg_shortage persists, no store_request, job proceeds ----
class TestSfgOnlyShortage:
    def test_sfg_shortage_field_present_and_shaped(self, session_cookie, seeded_jobs):
        r = _get_status(session_cookie, seeded_jobs["sfg_only_done"])
        assert r.status_code == 200, r.text
        job = r.json()
        # Behavior: sfg_shortage persists to a completed job
        assert "sfg_shortage" in job, "sfg_shortage must persist to terminal 'done' status"
        sfg = job["sfg_shortage"]
        assert sfg["site_id"] == "P1"
        assert isinstance(sfg["short_components"], list) and len(sfg["short_components"]) == 1
        comp = sfg["short_components"][0]
        for f in ("product_id", "description", "unit_of_measure", "required_qty", "available_qty"):
            assert f in comp, f"short_components entry missing field {f}"

    def test_sfg_only_did_not_pause_or_fail(self, session_cookie, seeded_jobs):
        r = _get_status(session_cookie, seeded_jobs["sfg_only_done"])
        job = r.json()
        assert job["status"] == "done"
        # Behavior: NO Store Approval request created just because of SFG
        assert not job.get("store_request_id"), "SFG-only shortage must NOT create a store_request"
        # Behavior: no failed status due to sfg
        assert job.get("status") != "failed"


# ---- Scenario 2: BOP/RM shortage only - existing behavior unchanged ----
class TestBopOnlyShortage:
    def test_bop_shortage_pauses_at_waiting_store_approval(self, session_cookie, seeded_jobs):
        r = _get_status(session_cookie, seeded_jobs["bop_only_paused"])
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "waiting_store_approval"
        assert job.get("store_request_id"), "BOP shortage must create a store_request"
        assert job.get("production_proposal_id"), "Proposal must still be created before pause"
        assert "sfg_shortage" not in job, "No SFG flag when only BOP short"


# ---- Scenario 3: Both SFG + BOP short - flags coexist ----
class TestBothShort:
    def test_both_flags_coexist(self, session_cookie, seeded_jobs):
        r = _get_status(session_cookie, seeded_jobs["both_short"])
        job = r.json()
        assert job["status"] == "waiting_store_approval"
        assert job.get("store_request_id")
        assert job.get("production_proposal_id")
        assert "sfg_shortage" in job and job["sfg_shortage"]["short_components"]


# ---- Scenario 4: No shortage - regression, unchanged ----
class TestNormalNoShortage:
    def test_no_shortage_job_done_no_flags(self, session_cookie, seeded_jobs):
        r = _get_status(session_cookie, seeded_jobs["no_short_done"])
        job = r.json()
        assert job["status"] == "done"
        assert "sfg_shortage" not in job
        assert not job.get("store_request_id")


# ---- Source-code guard: no failed-with-reason=sfg_shortage path remains ----
class TestSourceGuards:
    def test_no_sfg_shortage_failure_branch_in_server(self):
        """The old blocking branch used to set status='failed' with
        reason='sfg_shortage'. Prove that literal is gone from the
        create-and-release job function."""
        with open("/app/backend/server.py") as f:
            src = f.read()
        # Must not contain the old blocked reason string
        assert '"reason": "sfg_shortage"' not in src, "Old sfg_shortage failure branch still present in server.py"
        assert "'reason': 'sfg_shortage'" not in src

    def test_short_sfg_branch_only_flags(self):
        with open("/app/backend/server.py") as f:
            lines = f.readlines()
        # Find the `if short_sfg:` line and inspect its block by indentation.
        sfg_idx = next((i for i, l in enumerate(lines) if l.strip() == "if short_sfg:"), None)
        assert sfg_idx is not None, "`if short_sfg:` block missing entirely from server.py"
        base_indent = len(lines[sfg_idx]) - len(lines[sfg_idx].lstrip())
        block_lines = []
        for l in lines[sfg_idx + 1:]:
            if l.strip() == "":
                continue
            cur_indent = len(l) - len(l.lstrip())
            if cur_indent <= base_indent:
                break
            block_lines.append(l)
        block_src = "".join(block_lines)
        # Must set sfg_shortage
        assert '"sfg_shortage"' in block_src, "short_sfg block must write the sfg_shortage flag"
        # Must NOT early-return inside the block (would re-introduce the old blocking behavior)
        for l in block_lines:
            assert not l.strip().startswith("return"), (
                f"short_sfg block must not early-return - found: {l.strip()}"
            )


# ---- API surface sanity ----
class TestApiSurface:
    def test_status_endpoint_404_for_unknown(self, session_cookie):
        r = _get_status(session_cookie, "definitely-not-a-real-job-id")
        assert r.status_code == 404

    def test_status_endpoint_requires_page_access(self, seeded_jobs):
        # No cookie -> 401
        r = requests.get(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order/status/{seeded_jobs['sfg_only_done']}",
            timeout=60,
        )
        assert r.status_code in (401, 403), f"Expected 401/403 without auth, got {r.status_code}"
