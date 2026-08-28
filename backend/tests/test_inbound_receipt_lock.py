"""Inbound STO Receipt - STO-level job lock + server-side quantity validation
(iteration 131). No live SAP write happens here: the lock is exercised with a
seeded 'running' job doc, and the qty tests are rejected before job creation.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values, load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")
import job_store  # noqa: E402

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"]).rstrip("/")
COOKIES = {"vms_session": "d03966d2f366b13718f5cb3c914ac03b930792998e786be6"}


@pytest.fixture(scope="module")
def jobs():
    db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    return db[job_store.COLLECTION_NAME]


@pytest.fixture(scope="module")
def pending_sto():
    r = requests.get(f"{BASE_URL}/api/inbound-receipts/pending", cookies=COOKIES, timeout=180)
    assert r.status_code == 200, r.text[:300]
    orders = [o for o in r.json().get("orders", []) if o.get("receipt_status") == "pending" and o.get("items")]
    if not orders:
        pytest.skip("no pending STO available to test against")
    return orders[0]


def test_sto_level_lock_returns_same_job_id(jobs, pending_sto):
    sto_id = pending_sto["sto_id"]
    fake_id = "TESTLOCK-" + str(uuid.uuid4())
    # NOTE: background_jobs is shared with the STO-*creation* jobs, which also
    # carry an sto_id, so count relative to the pre-existing baseline.
    baseline = jobs.count_documents({"sto_id": sto_id})
    jobs.insert_one({
        "_id": fake_id, "sto_id": sto_id, "status": "running", "progress": "Starting...",
        "result": None, "error": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })
    try:
        line = pending_sto["items"][0]
        r = requests.post(
            f"{BASE_URL}/api/inbound-receipts/{sto_id}/receive",
            json={"items": [{"line_no": line["line_no"], "received_qty": line["requested_qty"]}]},
            cookies=COOKIES, timeout=60,
        )
        assert r.status_code == 200, r.text[:300]
        assert r.json().get("job_id") == fake_id
        assert jobs.count_documents({"sto_id": sto_id}) == baseline + 1
    finally:
        jobs.delete_one({"_id": fake_id})


def test_over_shipped_quantity_rejected_before_job(jobs, pending_sto):
    sto_id = pending_sto["sto_id"]
    line = pending_sto["items"][0]
    before = jobs.count_documents({"sto_id": sto_id})
    r = requests.post(
        f"{BASE_URL}/api/inbound-receipts/{sto_id}/receive",
        json={"items": [{"line_no": line["line_no"], "received_qty": line["requested_qty"] + 99}]},
        cookies=COOKIES, timeout=60,
    )
    assert r.status_code == 400
    assert "shipped quantity" in r.json()["detail"]
    assert jobs.count_documents({"sto_id": sto_id}) == before


def test_zero_quantity_rejected(jobs, pending_sto):
    sto_id = pending_sto["sto_id"]
    line = pending_sto["items"][0]
    before = jobs.count_documents({"sto_id": sto_id})
    r = requests.post(
        f"{BASE_URL}/api/inbound-receipts/{sto_id}/receive",
        json={"items": [{"line_no": line["line_no"], "received_qty": 0}]},
        cookies=COOKIES, timeout=60,
    )
    assert r.status_code == 400
    assert jobs.count_documents({"sto_id": sto_id}) == before
