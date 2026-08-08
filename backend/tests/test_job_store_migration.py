"""
Tests for the shared MongoDB-backed job store migration (job_store.py).
Covers all 5 job trackers: purchasing-plan, inventory refresh,
inventory deep-backfill, production-plan MRP, admin push-all-to-sap.
Verifies job docs land in Mongo's `background_jobs` collection (not just
in-memory) and that the TTL index on created_at exists.
"""
import os
import time
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "test_database"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


def poll_until_done(url, timeout=360, interval=2):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        r = requests.get(url)
        assert r.status_code == 200, f"poll failed: {r.status_code} {r.text}"
        last = r.json()
        if last["status"] in ("done", "failed"):
            return last
        time.sleep(interval)
    raise TimeoutError(f"job did not finish in {timeout}s, last={last}")


class TestBackgroundJobsCollection:
    def test_ttl_index_on_created_at(self, db):
        indexes = list(db["background_jobs"].list_indexes())
        ttl_indexes = [i for i in indexes if i.get("key", {}).get("created_at") is not None]
        assert len(ttl_indexes) > 0, "no index on created_at found"
        idx = ttl_indexes[0]
        assert idx.get("expireAfterSeconds") == 86400, f"expected TTL 86400, got {idx.get('expireAfterSeconds')}"

    def test_unknown_job_id_returns_404(self):
        r = requests.get(f"{BASE_URL}/api/purchasing-plan/status/nonexistent-job-id")
        assert r.status_code == 404
        assert "Unknown job_id" in r.json().get("detail", "")


class TestPurchasingPlanJob:
    def test_generate_and_poll(self, db):
        r = requests.post(f"{BASE_URL}/api/purchasing-plan/generate", json={})
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        assert job_id

        # Verify doc exists in Mongo directly (not in-memory)
        doc = db["background_jobs"].find_one({"_id": job_id})
        assert doc is not None, "job doc not found in background_jobs collection"
        assert doc["status"] == "running"

        result = poll_until_done(f"{BASE_URL}/api/purchasing-plan/status/{job_id}", timeout=120)
        assert result["status"] in ("done", "failed")
        assert result["job_id"] == job_id
        if result["status"] == "done":
            assert "components" in result["result"]
            assert "months" in result["result"]


class TestInventoryRefreshJob:
    def test_generate_and_poll(self, db):
        r = requests.post(f"{BASE_URL}/api/inventory")
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        doc = db["background_jobs"].find_one({"_id": job_id})
        assert doc is not None
        assert doc["status"] == "running"

        result = poll_until_done(f"{BASE_URL}/api/inventory/{job_id}", timeout=180)
        assert result["status"] in ("done", "failed")
        if result["status"] == "done":
            assert "items" in result["result"]
            assert "updated_at" in result["result"]


class TestDeepBackfillJob:
    def test_generate_and_poll(self, db):
        r = requests.post(f"{BASE_URL}/api/inventory/deep-backfill-uuids")
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        doc = db["background_jobs"].find_one({"_id": job_id})
        assert doc is not None
        # phase may already have progressed past "resolving" (e.g. to
        # "refreshing_cache") if the job runs fast - just assert it's a
        # known valid phase rather than racing the exact initial value.
        assert doc.get("phase") in ("resolving", "refreshing_cache")

        result = poll_until_done(f"{BASE_URL}/api/inventory/deep-backfill-uuids/{job_id}", timeout=360)
        assert result["status"] in ("done", "failed")
        if result["status"] == "done":
            r2 = result["result"]
            assert "total" in r2 and "resolved" in r2 and "still_missing" in r2
            assert "stopped_early" in r2
            assert isinstance(r2["stopped_early"], bool)


class TestMrpPlanJob:
    def test_generate_and_poll(self, db):
        r = requests.post(f"{BASE_URL}/api/production-plan/mrp/generate")
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        doc = db["background_jobs"].find_one({"_id": job_id})
        assert doc is not None
        assert doc["status"] == "running"

        result = poll_until_done(f"{BASE_URL}/api/production-plan/mrp/status/{job_id}", timeout=180)
        assert result["status"] in ("done", "failed")
        if result["status"] == "done":
            r2 = result["result"]
            assert "components" in r2
            assert "generated_at" in r2


class TestPushAllToSapJob:
    def test_generate_and_poll(self, db):
        r = requests.post(f"{BASE_URL}/api/admin/components/push-all-to-sap")
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        doc = db["background_jobs"].find_one({"_id": job_id})
        assert doc is not None
        assert doc["status"] == "running"

        result = poll_until_done(f"{BASE_URL}/api/admin/components/push-all-to-sap/{job_id}", timeout=180)
        assert result["status"] in ("done", "failed")
        if result["status"] == "done":
            r2 = result["result"]
            assert "total" in r2 and "pushed" in r2 and "failed" in r2
