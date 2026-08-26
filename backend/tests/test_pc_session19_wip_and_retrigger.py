"""Session (Aug 2026) fixes on the Production Confirmation page:
- POST /production-confirmation/history/latest-batch KeyError fix (legacy docs w/o reporting_point_id)
- POST /production-confirmation/retry-wip-clearing -> skipped:true when other ops still open (Lot 70563)
- NEW POST /production-confirmation/create-and-release-order/{job_id}/force-retrigger error handling
- read-only regression on open-lots / job history endpoints

READ-ONLY: this tenant writes to LIVE SAP. No confirmations / order creations here.
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
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

USER_ID = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-QA-OID-S19-" + (os.environ.get("PYTEST_XDIST_WORKER") or "main")
LOT = "70563"
SITE = "P2"


@pytest.fixture(scope="session")
def mongo_db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="session")
def api_client(mongo_db):
    token = "TESTQA19" + secrets.token_urlsafe(24)
    mongo_db.auth_users.replace_one(
        {"_id": USER_ID},
        {
            "_id": USER_ID,
            "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad",
            "oid": USER_ID.split(":")[1],
            "email": "qa.s19@rampgroup.co.in",
            "name": "QA S19",
            "role": "super_admin",
            "allowed_pages": ["production_confirmation"],
            "created_at": datetime.now(timezone.utc),
            "last_login_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    mongo_db.auth_sessions.insert_one(
        {"_id": token, "user_id": USER_ID, "expires_at": datetime.now(timezone.utc) + timedelta(hours=4)}
    )
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    session.cookies.set("vms_session", token, domain="sap-data-sync.preview.emergentagent.com")
    yield session
    mongo_db.auth_sessions.delete_one({"_id": token})
    mongo_db.auth_users.delete_one({"_id": USER_ID})


# --- history/latest-batch (KeyError regression) ---
class TestLatestBatch:
    def test_mixed_lots_returns_200(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/production-confirmation/history/latest-batch",
            json={"production_lot_ids": ["70563", "70539", "70538"]},
            timeout=60,
        )
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert isinstance(data, dict)
        for key, val in data.items():
            assert "::" in key, f"key not lot::rp -> {key}"
            assert "at" in val
            assert "_id" not in val

    def test_empty_list(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/production-confirmation/history/latest-batch",
            json={"production_lot_ids": []}, timeout=30,
        )
        assert r.status_code == 200
        assert r.json() == {}

    def test_legacy_doc_without_reporting_point_id_does_not_500(self, api_client, mongo_db):
        """Insert a legacy-shaped doc (no reporting_point_id) and confirm the
        whole batch still returns 200 with the other lots intact."""
        coll = mongo_db["production_confirmation_history"]
        doc_id = "TEST_legacy_" + secrets.token_hex(6)
        coll.insert_one({
            "_id": doc_id,
            "production_lot_id": LOT,
            "success": True,
            "at": datetime.now(timezone.utc),
            "actor": "TEST_QA",
        })
        try:
            r = api_client.post(
                f"{BASE_URL}/api/production-confirmation/history/latest-batch",
                json={"production_lot_ids": [LOT, "70539"]}, timeout=60,
            )
            assert r.status_code == 200, r.text[:500]
            data = r.json()
            assert all("::" in k for k in data)
        finally:
            coll.delete_one({"_id": doc_id})

    def test_badge_shape_for_skipped_wip(self, api_client, mongo_db):
        """A wip_clearing result with skipped:true must be surfaced as-is so
        the UI can render the neutral 'WIP Pending' chip (not red Failed)."""
        coll = mongo_db["production_confirmation_history"]
        doc_id = "TEST_skipwip_" + secrets.token_hex(6)
        coll.insert_one({
            "_id": doc_id,
            "production_lot_id": "TESTLOT999",
            "reporting_point_id": "RP10",
            "success": True,
            "wip_clearing": {"success": None, "skipped": True, "log": "still open"},
            "at": datetime.now(timezone.utc),
            "actor": "TEST_QA",
        })
        try:
            r = api_client.post(
                f"{BASE_URL}/api/production-confirmation/history/latest-batch",
                json={"production_lot_ids": ["TESTLOT999"]}, timeout=30,
            )
            assert r.status_code == 200, r.text[:300]
            entry = r.json().get("TESTLOT999::RP10")
            assert entry is not None
            assert entry["wip_clearing"]["skipped"] is True
            assert entry["wip_clearing"]["success"] is None
        finally:
            coll.delete_one({"_id": doc_id})


# --- retry-wip-clearing gating on multi-step routing ---
class TestRetryWipClearingGating:
    def test_lot_70563_is_skipped_not_fired(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/production-confirmation/retry-wip-clearing",
            json={"production_lot_id": LOT, "site_id": SITE, "actor": "TEST_QA"},
            timeout=180,
        )
        assert r.status_code in (200, 404), r.text[:500]
        if r.status_code == 404:
            pytest.skip("No confirmation history for Lot 70563 in this DB")
        wip = r.json()["wip_clearing"]
        assert wip["skipped"] is True
        assert wip["success"] is None
        assert "still open" in wip["log"].lower()

    def test_missing_actor_rejected(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/production-confirmation/retry-wip-clearing",
            json={"production_lot_id": LOT, "site_id": SITE, "actor": "  "}, timeout=60,
        )
        assert r.status_code == 400, r.text[:300]


# --- force-retrigger (new endpoint) ---
class TestForceRetrigger:
    def test_unknown_job_id(self, api_client):
        r = api_client.post(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order/does-not-exist-123/force-retrigger",
            timeout=60,
        )
        assert r.status_code in (400, 404), r.text[:300]
        assert "detail" in r.json()

    def test_completed_job_returns_400(self, api_client, mongo_db):
        job = mongo_db["background_jobs"].find_one(
            {"status": {"$in": ["completed", "failed", "success", "done"]}}
        )
        if not job:
            pytest.skip("No completed job present in job store to test against")
        r = api_client.post(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order/{job['_id']}/force-retrigger",
            timeout=60,
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:400]}"
        assert "waiting" in r.json()["detail"].lower()


# --- read-only regression on the page's own load endpoints ---
class TestReadOnlyRegression:
    def test_open_lots_loads(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/production-confirmation/open-lots?site_id={SITE}", timeout=240)
        assert r.status_code == 200, r.text[:400]
        rows = r.json().get("rows")
        assert isinstance(rows, list)
        for row in rows[:5]:
            assert "production_lot_id" in row
            assert "_id" not in row

    def test_lot_lookup_70563_multi_step(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/production-confirmation/lot/{LOT}", timeout=180)
        assert r.status_code == 200, r.text[:400]
        rows = r.json().get("rows") or []
        assert rows, "Lot 70563 returned no reporting points"
        assert len(rows) >= 2, f"expected multi-step routing, got {len(rows)} rows"
        finished = [bool(x.get("task_finished")) for x in rows]
        assert not all(finished), "expected at least one operation still open on Lot 70563"

    def test_proposal_history(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/production-confirmation/proposal-history", timeout=120)
        assert r.status_code == 200, r.text[:300]

    def test_confirmation_history(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/production-confirmation/history", timeout=120)
        assert r.status_code == 200, r.text[:300]
