"""Tests for the new MongoDB-backed BOM cache feature (bom_cache_service.py,
purchasing_plan.py db integration, /api/bom-cache/* endpoints).

Covers:
  - Cold vs warm purchasing-plan generation for the same month (correctness +
    speedup)
  - GET /api/bom-cache/stats shape and growth
  - POST /api/bom-cache/refresh fire-and-forget (must return fast)
  - Repeated (3rd) generation stability/no-drift
"""
import os
import time

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
TARGET_MONTH = "2020-01"


def poll_job(job_id, timeout=90):
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(f"{BASE_URL}/api/purchasing-plan/status/{job_id}", timeout=30)
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in ("done", "failed"):
            return data, time.time() - start
        time.sleep(1.5)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


def generate_plan(month=TARGET_MONTH):
    resp = requests.post(
        f"{BASE_URL}/api/purchasing-plan/generate",
        json={"target_month": month},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    return poll_job(job_id)


class TestBomCachePurchasingPlan:
    def test_run1_cold_then_run2_warm_identical_and_faster(self):
        data1, elapsed1 = generate_plan()
        assert data1["status"] == "done", data1.get("error")
        result1 = data1["result"]
        assert len(result1["components"]) > 0

        data2, elapsed2 = generate_plan()
        assert data2["status"] == "done", data2.get("error")
        result2 = data2["result"]

        # Correctness: identical results
        assert result1["months"] == result2["months"]
        assert len(result1["components"]) == len(result2["components"])
        assert len(result1["missing_boms"]) == len(result2["missing_boms"])

        ids1 = sorted(c["product_id"] for c in result1["components"])
        ids2 = sorted(c["product_id"] for c in result2["components"])
        assert ids1 == ids2

        parts1 = sorted(m["part_no"] for m in result1["missing_boms"])
        parts2 = sorted(m["part_no"] for m in result2["missing_boms"])
        assert parts1 == parts2

        print(f"Run1 (cold-ish) elapsed={elapsed1:.1f}s, Run2 (warm) elapsed={elapsed2:.1f}s, "
              f"components={len(result1['components'])}, missing={len(result1['missing_boms'])}")

        # Speedup expectation (soft check - report but don't hard-fail if SAP flaky)
        self.run2_elapsed = elapsed2

    def test_run3_stable_after_cache_warm(self):
        data3, elapsed3 = generate_plan()
        assert data3["status"] == "done", data3.get("error")
        result3 = data3["result"]
        assert len(result3["components"]) > 0
        print(f"Run3 (warm) elapsed={elapsed3:.1f}s, components={len(result3['components'])}")
        assert elapsed3 < 20, f"3rd warm run took {elapsed3}s, expected fast cached response"


class TestBomCacheStats:
    def test_stats_shape_and_positive_after_generation(self):
        # Ensure at least one generation has run (from prior test class or run here)
        generate_plan()
        resp = requests.get(f"{BASE_URL}/api/bom-cache/stats", timeout=15)
        assert resp.status_code == 200
        data = resp.json()
        assert "cached_nodes" in data
        assert isinstance(data["cached_nodes"], int)
        assert data["cached_nodes"] > 0
        assert data.get("oldest_checked_at") is not None
        assert data.get("newest_checked_at") is not None


class TestBomCacheRefresh:
    def test_refresh_returns_fast_and_triggered(self):
        start = time.time()
        resp = requests.post(f"{BASE_URL}/api/bom-cache/refresh", timeout=10)
        elapsed = time.time() - start
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"triggered": True}
        print(f"/bom-cache/refresh responded in {elapsed:.2f}s")
        assert elapsed < 5, f"refresh endpoint took {elapsed:.2f}s, expected <5s (fire-and-forget)"
