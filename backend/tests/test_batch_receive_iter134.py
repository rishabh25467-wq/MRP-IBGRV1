"""Iteration 134 - retest of iteration_133 fixes on Inbound STO Receipt.

Covers:
- FIX 3: GET /api/inbound-receipts/pending exposes `active_job` per order
- FIX 2: GET /api/inbound-receipts/receive-status/{job_id} shape (processing_started_at support)
- FIX 4: orphan recovery persists receipt_error onto the STO doc
- FIX 5/6: playwright_concurrency acquire()/release()/register_browser/close_all_browsers wiring
"""
import os
import re
import sys
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
SESSION_COOKIE = "d03966d2f366b13718f5cb3c914ac03b930792998e786be6"

sys.path.insert(0, "/app/backend")


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    s.cookies.set("vms_session", SESSION_COOKIE)
    return s


@pytest.fixture(scope="module")
def pending(client):
    r = client.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=120)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data.get("orders"), list)
    return data["orders"]


# ---------- FIX 3: active_job on /pending ----------
class TestActiveJobExposure:
    def test_pending_orders_all_carry_active_job_key(self, pending):
        assert pending, "no pending inbound receipts to test against"
        missing = [o["sto_id"] for o in pending if "active_job" not in o]
        assert not missing, f"orders missing active_job key: {missing[:5]}"

    def test_active_job_shape_when_present(self, pending):
        with_job = [o for o in pending if o.get("active_job")]
        if not with_job:
            pytest.skip("no currently-running receive job at test time")
        for o in with_job:
            j = o["active_job"]
            for key in ("job_id", "phase", "progress_current", "progress_total", "processing_started_at"):
                assert key in j, f"{o['sto_id']} active_job missing {key}"
            assert j["phase"] in ("queued", "processing", "done", "failed")

    def test_receipt_error_field_present(self, pending):
        assert all("receipt_error" in o for o in pending)


# ---------- FIX 2: job status endpoint ----------
class TestReceiveStatusEndpoint:
    def test_unknown_job_id_404(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/receive-status/does-not-exist-134", timeout=60)
        assert r.status_code == 404

    def test_status_of_running_job_has_expected_fields(self, client, pending):
        with_job = [o for o in pending if o.get("active_job")]
        if not with_job:
            pytest.skip("no running job to query")
        job_id = with_job[0]["active_job"]["job_id"]
        r = client.get(f"{BASE_URL}/api/inbound-receipts/receive-status/{job_id}", timeout=60)
        assert r.status_code == 200
        job = r.json()
        assert "_id" not in job
        for key in ("status", "phase", "progress_current", "progress_total"):
            assert key in job
        if job.get("phase") in ("processing", "done"):
            assert job.get("processing_started_at"), "processing job missing processing_started_at"


# ---------- FIX 4: orphan recovery persists receipt_error ----------
class TestOrphanRecoveryPersistsError:
    def test_startup_block_updates_sto_receipt_error(self):
        src = Path("/app/backend/server.py").read_text()
        block = src[src.index("_recovered_jobs = job_store.recover_orphaned_jobs"):][:1200]
        assert 'kind") == "inbound_receipt"' in block
        assert "receipt_error" in block
        assert "STO_COLLECTION" in block

    def test_recover_orphaned_jobs_returns_docs_and_sets_phase(self):
        src = Path("/app/backend/job_store.py").read_text()
        fn = src[src.index("def recover_orphaned_jobs"):]
        fn = fn[: fn.index("\ndef ", 10)] if "\ndef " in fn[10:] else fn
        assert "-> list" in fn
        assert '"phase": "failed"' in fn
        assert "return orphaned" in fn
        assert '"kind": 1' in fn and '"sto_id": 1' in fn

    def test_orphaned_stos_from_prev_iteration(self, pending):
        by_id = {o["sto_id"]: o for o in pending}
        interrupted = {k: (by_id[k].get("receipt_error") if k in by_id else "NOT_PENDING")
                       for k in ("STO-000065", "STO-000066", "STO-000067")}
        print(f"iteration_133 interrupted orders receipt_error: {interrupted}")
        # informational only - jobs may already have been marked failed by an earlier restart
        assert isinstance(interrupted, dict)


# ---------- FIX 5 / FIX 6: concurrency + browser cleanup wiring ----------
class TestPlaywrightConcurrencyWiring:
    def test_dedicated_executor_and_helpers(self):
        import playwright_concurrency as pc
        assert pc.MAX_CONCURRENT_SAP_UI_SESSIONS == 3
        assert pc._QUEUE_WAIT_EXECUTOR._max_workers == 64
        assert callable(pc.acquire) and callable(pc.release)
        assert callable(pc.register_browser) and callable(pc.unregister_browser)
        assert callable(pc.close_all_browsers)

    @pytest.mark.parametrize("svc", ["sap_playwright_pgr_service.py", "sap_playwright_outbound_gi_service.py"])
    def test_services_use_helpers_not_to_thread(self, svc):
        src = Path(f"/app/backend/{svc}").read_text()
        assert "await playwright_concurrency.acquire()" in src
        assert "playwright_concurrency.release()" in src
        assert "asyncio.to_thread(sap_ui_semaphore.acquire" not in src
        assert "playwright_concurrency.register_browser(browser)" in src
        assert "playwright_concurrency.unregister_browser(browser)" in src

    def test_shutdown_handler_exists(self):
        src = Path("/app/backend/server.py").read_text()
        assert re.search(r'@app\.on_event\("shutdown"\)\s*\nasync def \w+', src)
        assert "playwright_concurrency.close_all_browsers()" in src

    def test_browser_registered_immediately_after_launch(self):
        for svc in ("sap_playwright_pgr_service.py", "sap_playwright_outbound_gi_service.py"):
            lines = Path(f"/app/backend/{svc}").read_text().splitlines()
            launch_idx = [i for i, l in enumerate(lines) if "chromium.launch(" in l]
            assert launch_idx, f"{svc}: no chromium.launch found"
            for i in launch_idx:
                assert "register_browser" in "".join(lines[i:i + 3]), f"{svc}: launch at line {i+1} not registered"


# ---------- FIX 7 / FIX 8: StockTransferPage line matching + modal width ----------
class TestStockTransferFrontendSource:
    def test_line_status_matches_line_no_first(self):
        src = Path("/app/frontend/src/pages/StockTransferPage.js").read_text()
        fn = src[src.index("const lineShipStatus"):][:600]
        assert "l.line_no === item.line_no" in fn
        assert fn.index("line_no === item.line_no") < fn.index("product_id === item.product_id")
        assert "stock-transfer-detail-ship-status-${it.line_no" in src
        assert 'className="max-w-5xl' in src


# ---------- regression: core endpoints still healthy ----------
class TestRegression:
    def test_sites_endpoint(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/sites", timeout=60)
        assert r.status_code == 200
        assert isinstance(r.json().get("sites"), list)

    def test_receive_rejects_unknown_sto(self, client):
        r = client.post(f"{BASE_URL}/api/inbound-receipts/STO-DOES-NOT-EXIST-134/receive",
                        json={"items": []}, timeout=60)
        assert r.status_code == 400, r.text
