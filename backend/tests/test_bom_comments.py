"""Tests for the new BOM Attachment Comments feature:
- GET /api/bom/comments (new endpoint, read-only cache lookup)
- GET /api/bom/drawing-urls (regression check - underlying resolve_material_info() was modified)

Requires an authenticated session (vms_session cookie). A temporary
super_admin session is created/torn down within this module.
"""
import os
import secrets
import datetime

import pytest
import requests
import pymongo
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback: read from frontend/.env directly
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.strip().split("=", 1)[1].rstrip("/")

MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME")


@pytest.fixture(scope="module")
def session_token():
    client = pymongo.MongoClient(MONGO_URL)
    db = client[DB_NAME]
    user_id = "TESTTID:TESTOID_pytest_bomcomment"
    token = secrets.token_hex(24)
    db["auth_users"].update_one(
        {"_id": user_id},
        {"$set": {
            "_id": user_id, "tid": "TESTTID", "oid": "TESTOID_pytest_bomcomment",
            "email": "pytest_bomcomment@rampgroup.co.in", "name": "Pytest BomComment",
            "role": "super_admin", "allowed_pages": [],
            "created_at": datetime.datetime.utcnow(), "last_login_at": datetime.datetime.utcnow(),
        }},
        upsert=True,
    )
    db["auth_sessions"].update_one(
        {"_id": token},
        {"$set": {"_id": token, "user_id": user_id,
                   "expires_at": datetime.datetime.utcnow() + datetime.timedelta(hours=1)}},
        upsert=True,
    )
    yield token
    db["auth_sessions"].delete_one({"_id": token})
    db["auth_users"].delete_one({"_id": user_id})
    client.close()


@pytest.fixture
def api_client(session_token):
    session = requests.Session()
    session.cookies.set("vms_session", session_token)
    return session


class TestBomComments:
    def test_comments_known_real_id(self, api_client):
        resp = api_client.get(f"{BASE_URL}/api/bom/comments", params={"product_ids": "5989828-12"})
        assert resp.status_code == 200
        data = resp.json()
        assert "5989828-12" in data
        entries = data["5989828-12"]
        assert isinstance(entries, list) and len(entries) == 1
        entry = entries[0]
        assert entry["title"] == "Document"
        assert entry["type_code"] == "10001"
        assert entry["type_label"] == "Standard Attachment"
        assert entry["comment"] == (
            "ECR No. 83 raised to correct the Marked identification of Left and Right Arm."
        )

    def test_comments_nonexistent_id_returns_empty_object(self, api_client):
        resp = api_client.get(f"{BASE_URL}/api/bom/comments", params={"product_ids": "NONEXISTENT-ID-XYZ"})
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_drawing_urls_regression(self, api_client):
        resp = api_client.get(f"{BASE_URL}/api/bom/drawing-urls", params={"product_ids": "5989828-12"})
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"5989828-12": "https://rampgroup.net/Documents.aspx?mid=5989828-12"}

    def test_comments_unauthenticated_rejected(self):
        resp = requests.get(f"{BASE_URL}/api/bom/comments", params={"product_ids": "5989828-12"})
        assert resp.status_code in (401, 403)
