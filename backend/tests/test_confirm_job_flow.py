"""Aug 2026 - Production Confirmation background-job + polling flow.

Covers the fix that converted POST /api/production-confirmation/confirm from a
fully-synchronous (up to 4 sequential SOAP calls) request into a background job
returning {job_id} immediately, polled via
GET /api/production-confirmation/confirm/status/{job_id}.

SAFETY: only FAKE/zero UUIDs are ever submitted so SAP always rejects the
posting - no real confirmation / WIP clearing is ever written to the live tenant.
"""
import os
import time

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL is missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

FAKE_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def client():
    """Entra-SSO protected app - create a synthetic Mongo session (documented
    technique in /app/memory/test_credentials.md) and clean it up afterwards."""
    import secrets
    from datetime import datetime, timedelta, timezone

    from pymongo import MongoClient

    env = dotenv_values("/app/backend/.env")
    mongo = MongoClient(os.environ.get("MONGO_URL") or env["MONGO_URL"])
    db = mongo[os.environ.get("DB_NAME") or env["DB_NAME"]]
    user_id = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-QA-PYTEST"
    token = "TESTQA" + secrets.token_urlsafe(24)
    db.auth_users.replace_one({"_id": user_id}, {
        "_id": user_id, "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad", "oid": "TEST-QA-PYTEST",
        "email": "qa.pytest@rampgroup.co.in", "name": "QA Pytest", "role": "super_admin",
        "allowed_pages": ["production_confirmation"],
        "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db.auth_sessions.insert_one({
        "_id": token, "user_id": user_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=2),
    })

    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    s.cookies.set("vms_session", token)
    yield s
    db.auth_sessions.delete_one({"_id": token})
    db.auth_users.delete_one({"_id": user_id})
    mongo.close()


# --- confirm status endpoint ---
def test_confirm_status_unknown_job_returns_404(client):
    r = client.get(f"{API}/production-confirmation/confirm/status/TEST_unknown-job-id-123", timeout=30)
    assert r.status_code == 404
    assert r.json().get("detail") == "Unknown job_id"


# --- confirm endpoint returns instantly (no ingress timeout) ---
def test_confirm_returns_job_id_instantly_and_job_fails_cleanly(client):
    payload = {
        "production_lot_id": "TEST_FAKE_LOT",
        "production_lot_uuid": FAKE_UUID,
        "confirmation_group_uuid": FAKE_UUID,
        "reporting_point_uuid": FAKE_UUID,
        "reporting_point_id": "END",
        "main_output_product": "TEST_FAKE_PRODUCT",
        "unit_code": "EA",
        "production_task_id": "TEST_FAKE_TASK",
        "production_task_uuid": FAKE_UUID,
        "confirmed_quantity": 1,
        "confirmed_scrap": 0,
        "deviation_reason_code": None,
        "confirmation_finished": True,
        "site_id": "P1",
        "actor": "QA Tester",
    }
    t0 = time.time()
    r = client.post(f"{API}/production-confirmation/confirm", json=payload, timeout=60)
    elapsed = time.time() - t0
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id")
    assert isinstance(job_id, str) and job_id
    # must respond well within the platform ingress timeout
    assert elapsed < 5, f"confirm endpoint took {elapsed:.2f}s - should return immediately"

    # poll until terminal
    status = None
    job = None
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        sr = client.get(f"{API}/production-confirmation/confirm/status/{job_id}", timeout=30)
        assert sr.status_code == 200, sr.text
        job = sr.json()
        assert "_id" not in job
        status = job.get("status")
        if status in ("done", "failed"):
            break
    assert status in ("failed", "done"), f"job never reached a terminal state: {job}"
    if status == "failed":
        err = job.get("error") or ""
        assert err, "failed job must carry a human-readable error message"
        assert "<html" not in err.lower()
        assert "TEST_FAKE_LOT" in err or "SAP" in err
        print("Clarified error:", err)
    else:
        # SAP business rejection path: status=done, result.success False + logs
        result = job.get("result") or {}
        assert result.get("success") is False, f"a fake-UUID posting must never succeed: {result}"
        logs = result.get("logs") or []
        assert logs and any(l.get("note") for l in logs), f"rejection must carry logs: {result}"
        print("SAP rejection logs:", logs)


# --- create-and-release-order cancel endpoint ---
def test_cancel_unknown_order_job(client):
    r = client.post(
        f"{API}/production-confirmation/create-and-release-order/TEST_unknown-job-999/cancel", timeout=30
    )
    assert r.status_code in (200, 404), r.text
    print("cancel unknown job ->", r.status_code, r.text[:200])
