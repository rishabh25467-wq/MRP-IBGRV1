"""Targeted regression test for the stale BOM cache timestamp fix.

The fix: `SAPSoapBOMClient._safe_fetch_boms_batch` now backfills
`result.setdefault(pid, None)` on a SUCCESSFUL parsed batch response for every
requested id SAP's response omitted (SAP silently drops ids with no BOM).
Callers can now distinguish:

  * "SAP confirmed no BOM"   -> pid present in dict with value None
  * "batch genuinely failed" -> pid absent from the dict entirely (still {})

Previously both looked identical (absent), so `bom_cache_service.refresh_stale
_nodes()` treated no-BOM leaf/raw-material items as permanent failures and
never advanced their `last_checked_at`.

Tests:
  1. Unit: successful batch backfills None for omitted ids.
  2. Unit: successful batch preserves actual BOM dicts for hit ids AND
     backfills None for the misses in the same call.
  3. Unit: all-attempts-fail batch returns {} (NOT {pid: None} for those ids).
  4. Unit: retry policy - one transient failure then success still backfills.
  5. Integration: MongoDB `bom_node_cache` collection - every `found: False`
     doc has a recent `last_checked_at` (the bug's real-world symptom).
  6. Integration/regression: light API smoke pass on `/api/bom-cache/stats`,
     `/api/bom-cache/refresh`, `/api/bom/search` for a known BOM ID.
"""
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests
from dotenv import dotenv_values

sys.path.insert(0, "/app/backend")

from sap_soap_client import SAPSoapBOMClient, SAPSoapError  # noqa: E402

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

backend_env = dotenv_values("/app/backend/.env")
MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")


@pytest.fixture(scope="module")
def auth_session():
    """Insert a synthetic super_admin session into MongoDB (per
    /app/memory/test_credentials.md 'Testing without a real Microsoft
    account') and yield a cookies dict for requests.* calls. Cleans up
    on teardown so no test accounts are left in the DB."""
    if not MONGO_URL or not DB_NAME:
        pytest.skip("MONGO_URL/DB_NAME missing")
    from pymongo import MongoClient

    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    user_id = f"TEST_TID:TEST_OID_{secrets.token_hex(4)}"
    session_id = f"TEST_{secrets.token_urlsafe(24)}"
    now = datetime.now(timezone.utc)
    db["auth_users"].insert_one({
        "_id": user_id,
        "tid": "TEST_TID",
        "oid": user_id.split(":", 1)[1],
        "email": "TEST_qa@example.test",
        "name": "TEST QA Agent",
        "role": "super_admin",
        "allowed_pages": [],
        "created_at": now,
        "last_login_at": now,
    })
    db["auth_sessions"].insert_one({
        "_id": session_id,
        "user_id": user_id,
        "expires_at": now + timedelta(hours=2),
    })
    try:
        yield {"vms_session": session_id}
    finally:
        db["auth_sessions"].delete_one({"_id": session_id})
        db["auth_users"].delete_one({"_id": user_id})
        client.close()


# ---------- Unit tests: _safe_fetch_boms_batch backfill behavior ----------

@pytest.fixture
def client():
    return SAPSoapBOMClient(endpoint="http://unused", username="u", password="p")


class TestSafeFetchBomsBatchBackfill:
    def test_successful_batch_backfills_none_for_omitted_ids(self, client):
        """SAP omits ids with no BOM; the fix must backfill them as None."""
        requested = ["P1", "P2", "P3", "P4"]
        # Simulate: SAP returned a BOM only for P2 (P1/P3/P4 are leaves)
        parsed_response = {"P2": {"bom_id": "P2_1", "groups": [{"items": []}]}}

        with patch.object(client, "_fetch_boms_by_output_products_batch",
                          return_value=parsed_response):
            result = client._safe_fetch_boms_batch(requested)

        assert set(result.keys()) == set(requested), (
            f"Expected all requested ids present in result, got {set(result.keys())}"
        )
        assert result["P2"] == parsed_response["P2"], "Actual hit must be preserved"
        assert result["P1"] is None
        assert result["P3"] is None
        assert result["P4"] is None

    def test_successful_batch_all_misses_backfills_all_none(self, client):
        """Common case for raw-materials-only chunk: all ids have no BOM."""
        requested = ["RAW1", "RAW2", "RAW3"]
        with patch.object(client, "_fetch_boms_by_output_products_batch",
                          return_value={}):
            result = client._safe_fetch_boms_batch(requested)

        assert set(result.keys()) == set(requested)
        assert all(v is None for v in result.values()), (
            "Every id in a fully-empty successful response must be None (not absent)"
        )

    def test_failed_batch_returns_empty_dict_not_none_values(self, client):
        """Genuinely failed batch (all retries raise) must return {} - NOT
        {pid: None} - so the caller knows it's a transient error to retry,
        not a confirmed-no-BOM."""
        requested = ["P1", "P2"]
        with patch.object(client, "_fetch_boms_by_output_products_batch",
                          side_effect=SAPSoapError("connection error")):
            with patch("sap_soap_client.time.sleep"):  # skip 1.5s backoff
                result = client._safe_fetch_boms_batch(requested)

        assert result == {}, (
            f"On total failure the batch must return {{}} (so caller retries "
            f"next cycle), got {result!r}"
        )
        # Critical: NEITHER id should appear as None - that would mis-flag
        # them as "confirmed no BOM" and advance their last_checked_at.
        assert "P1" not in result
        assert "P2" not in result

    def test_transient_failure_then_success_still_backfills(self, client):
        """First attempt raises SAPSoapError, second succeeds - the successful
        attempt's result must still be backfilled with None for omitted ids."""
        requested = ["P1", "P2"]
        # First call raises, second returns partial response
        call_results = [SAPSoapError("transient"), {"P1": {"bom_id": "P1_1", "groups": []}}]

        def side_effect(_ids):
            r = call_results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(client, "_fetch_boms_by_output_products_batch",
                          side_effect=side_effect):
            with patch("sap_soap_client.time.sleep"):
                result = client._safe_fetch_boms_batch(requested)

        assert set(result.keys()) == {"P1", "P2"}
        assert result["P1"] == {"bom_id": "P1_1", "groups": []}
        assert result["P2"] is None  # backfilled on the successful 2nd attempt

    def test_backfill_does_not_overwrite_real_hits(self, client):
        """setdefault semantics: must not clobber actual BOM data for ids that
        WERE in the response."""
        requested = ["P1", "P2"]
        real_bom = {"bom_id": "P1_REV3", "groups": [{"items": [{"product_id": "X"}]}]}
        with patch.object(client, "_fetch_boms_by_output_products_batch",
                          return_value={"P1": real_bom}):
            result = client._safe_fetch_boms_batch(requested)

        assert result["P1"] is real_bom or result["P1"] == real_bom
        assert result["P2"] is None


# ---------- Integration: MongoDB & API smoke ----------

class TestNoStaleFoundFalseCache:
    """The real-world symptom of the bug: `found: False` docs in the
    bom_node_cache collection whose last_checked_at is days old. After the
    fix + a refresh cycle, none should remain."""

    def test_no_bom_leaf_nodes_have_recent_last_checked_at(self):
        try:
            from pymongo import MongoClient
        except ImportError:
            pytest.skip("pymongo not installed")

        if not MONGO_URL or not DB_NAME:
            pytest.skip("MONGO_URL/DB_NAME missing")

        client = MongoClient(MONGO_URL)
        try:
            db = client[DB_NAME]
            coll = db["bom_node_cache"]
            total_no_bom = coll.count_documents({"found": False})
            if total_no_bom == 0:
                pytest.skip("No `found: False` docs present in cache yet")

            # A 6-hourly refresh with the fix in place should keep every
            # no-BOM leaf's last_checked_at well within the last 2 days.
            # (Previous agent reported 0 stale after fix; giving a 2-day
            # window absorbs a missed cycle without false alarms.)
            two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
            stale = coll.count_documents({
                "found": False,
                "last_checked_at": {"$lt": two_days_ago},
            })
            oldest = coll.find_one({"found": False}, sort=[("last_checked_at", 1)])
            oldest_ts = oldest.get("last_checked_at") if oldest else None
            print(f"bom_node_cache found:False total={total_no_bom}, "
                  f"stale(>2d)={stale}, oldest_last_checked_at={oldest_ts}")
            assert stale == 0, (
                f"Found {stale} `found: False` docs older than 2 days - "
                f"the stale-timestamp bug appears to still be present. "
                f"Oldest: {oldest_ts}"
            )
        finally:
            client.close()


class TestApiRegression:
    def test_bom_cache_stats_endpoint(self, auth_session):
        r = requests.get(f"{BASE_URL}/api/bom-cache/stats", cookies=auth_session, timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "cached_nodes" in data
        assert isinstance(data["cached_nodes"], int)
        assert data["cached_nodes"] > 0, "Expected a warm cache with >0 nodes"
        assert data.get("oldest_checked_at") is not None
        assert data.get("newest_checked_at") is not None
        print(f"cache stats: {data}")

    def test_bom_cache_refresh_fire_and_forget(self, auth_session):
        start = time.time()
        r = requests.post(f"{BASE_URL}/api/bom-cache/refresh", cookies=auth_session, timeout=30)
        elapsed = time.time() - start
        assert r.status_code == 200
        assert r.json() == {"triggered": True}
        assert elapsed < 10, f"refresh returned in {elapsed:.2f}s, must be fire-and-forget"

    def test_bom_search_known_id_still_works(self, auth_session):
        """Light regression on the live (non-cached) explode path that shares
        the same low-level `sap_soap_client` change."""
        for candidate in ("8060522_1", "FLT2_4.1"):
            r = requests.get(
                f"{BASE_URL}/api/bom/search",
                params={"bom_id": candidate},
                cookies=auth_session,
                timeout=120,
            )
            if r.status_code == 200:
                data = r.json()
                print(f"bom/search {candidate}: keys={list(data.keys())[:6]}")
                assert isinstance(data, dict)
                return
            else:
                print(f"bom/search {candidate}: HTTP {r.status_code} {r.text[:200]}")
        pytest.skip("Neither known BOM id resolved with 200 - live SAP may be down")
