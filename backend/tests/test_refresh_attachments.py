"""Tests for POST /api/bom/refresh-attachments (on-demand SAP attachment/ECN refresh).
Sets up a temporary super_admin session via direct MongoDB insert (Entra ID SSO app,
no end-user login form). Cleans up session+user docs at teardown per project convention.
"""
import os
import time
import uuid
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

TEST_USER_ID = "TEST_tid:TEST_oid_refresh_attach"
TEST_EMAIL = "TEST_refresh_attach_tester@rampgroup.co.in"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def session_cookie(db):
    token = f"TEST_session_{uuid.uuid4().hex}"
    now = time.time()
    db["auth_users"].insert_one({
        "_id": TEST_USER_ID,
        "tid": "TEST_tid",
        "oid": "TEST_oid_refresh_attach",
        "email": TEST_EMAIL,
        "name": "Test Refresh Attachments",
        "role": "super_admin",
        "allowed_pages": ["bom_explorer"],
        "created_at": now,
        "last_login_at": now,
    })
    db["auth_sessions"].insert_one({
        "_id": token,
        "user_id": TEST_USER_ID,
        "expires_at": now + 3600,
    })
    yield token
    db["auth_sessions"].delete_one({"_id": token})
    db["auth_users"].delete_one({"_id": TEST_USER_ID})


@pytest.fixture
def api_client(session_cookie):
    session = requests.Session()
    session.cookies.set("vms_session", session_cookie)
    session.headers.update({"Content-Type": "application/json"})
    return session


class TestRefreshAttachments:
    def test_auth_me_sanity(self, api_client):
        resp = api_client.get(f"{BASE_URL}/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("email") == TEST_EMAIL

    def test_empty_product_ids_returns_zero_immediately(self, api_client):
        start = time.time()
        resp = api_client.post(f"{BASE_URL}/api/bom/refresh-attachments", json={"product_ids": []})
        elapsed = time.time() - start
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"checked": 0, "found": 0, "failed": 0}
        assert elapsed < 5, f"Empty list should return immediately, took {elapsed}s"

    def test_single_known_product_id(self, api_client):
        start = time.time()
        resp = api_client.post(
            f"{BASE_URL}/api/bom/refresh-attachments",
            json={"product_ids": ["5989828-12"]},
        )
        elapsed = time.time() - start
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("checked") == 1
        assert data.get("failed") == 0
        assert isinstance(data.get("found"), int)
        print(f"single-id refresh took {elapsed:.2f}s -> {data}")

    def test_no_auth_rejected(self):
        session = requests.Session()
        resp = session.post(f"{BASE_URL}/api/bom/refresh-attachments", json={"product_ids": []})
        assert resp.status_code in (401, 403)
