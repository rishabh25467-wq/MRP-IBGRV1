"""Iteration 133 - Batch Inbound Receiving (queued/processing phases),
Playwright concurrency semaphore, and per-line gi_line_status.

Safe-by-design: NO test here triggers a real SAP Goods Receipt write.
Only validation/404/read-path checks plus in-process unit tests.
"""
import os
import sys
import threading
import time

import pytest
import requests
from dotenv import dotenv_values

sys.path.insert(0, "/app/backend")

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
SESSION_COOKIE = "d03966d2f366b13718f5cb3c914ac03b930792998e786be6"


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- module: server.py /api/inbound-receipts/* read paths ----------
class TestInboundReceiptReadPaths:
    def test_sites(self, api):
        r = api.get(f"{BASE_URL}/api/inbound-receipts/sites", timeout=90)
        assert r.status_code == 200, r.text[:300]
        sites = r.json()["sites"]
        assert isinstance(sites, list) and all(isinstance(s, str) for s in sites)

    def test_pending_shape(self, api):
        r = api.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=180)
        assert r.status_code == 200, r.text[:300]
        orders = r.json()["orders"]
        assert isinstance(orders, list)
        if not orders:
            pytest.skip("No pending inbound receipts in this environment")
        o = orders[0]
        for f in ("sto_id", "sap_order_id", "receipt_status", "items", "outbound_delivery_ids"):
            assert f in o, f"missing {f} in pending order payload"
        assert "_id" not in o, "raw mongo _id leaked in /pending"
        assert "receipt_error" in o, "receipt_error field must be exposed for the row badge/tooltip"
        assert o["items"] and {"line_no", "product_id", "requested_qty"} <= set(o["items"][0])

    def test_receive_status_unknown_job_404(self, api):
        r = api.get(f"{BASE_URL}/api/inbound-receipts/receive-status/does-not-exist", timeout=60)
        assert r.status_code == 404, r.text[:300]
        assert "detail" in r.json()


# ---------- module: POST /api/inbound-receipts/{sto_id}/receive validation ----------
class TestReceiveValidation:
    """Only invalid payloads / unknown ids -> must be rejected BEFORE any
    background job (and therefore any real SAP write) is started."""

    @pytest.fixture(scope="class")
    def pending_order(self, api):
        r = api.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=180)
        orders = r.json().get("orders") or []
        if not orders:
            pytest.skip("No pending inbound receipts available")
        return orders[0]

    def test_unknown_sto_400(self, api):
        r = api.post(f"{BASE_URL}/api/inbound-receipts/STO-DOESNOTEXIST/receive", json={"items": []}, timeout=90)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:300]}"

    def test_qty_over_shipped_rejected(self, api, pending_order):
        line = pending_order["items"][0]
        r = api.post(
            f"{BASE_URL}/api/inbound-receipts/{pending_order['sto_id']}/receive",
            json={"items": [{"line_no": line["line_no"], "received_qty": line["requested_qty"] + 5}]},
            timeout=120,
        )
        assert r.status_code == 400, f"over-shipment must be rejected, got {r.status_code}: {r.text[:300]}"
        assert "between 0 and the shipped quantity" in r.json()["detail"]

    def test_qty_zero_rejected(self, api, pending_order):
        line = pending_order["items"][0]
        r = api.post(
            f"{BASE_URL}/api/inbound-receipts/{pending_order['sto_id']}/receive",
            json={"items": [{"line_no": line["line_no"], "received_qty": 0}]},
            timeout=120,
        )
        assert r.status_code == 400, f"zero qty must be rejected, got {r.status_code}: {r.text[:300]}"

    def test_no_job_created_by_rejected_requests(self, api, pending_order):
        """A rejected request must not leave a running job behind."""
        from pymongo import MongoClient
        backend_env = dotenv_values("/app/backend/.env")
        cli = MongoClient(backend_env["MONGO_URL"])
        db = cli[backend_env["DB_NAME"]]
        running = db["background_jobs"].count_documents(
            {"sto_id": pending_order["sto_id"], "kind": "inbound_receipt", "status": "running"}
        )
        assert running == 0, f"rejected receive request left {running} running job(s)"
        cli.close()


# ---------- module: playwright_concurrency.py ----------
class TestPlaywrightConcurrency:
    def test_cap_is_three(self):
        import playwright_concurrency as pc
        assert pc.MAX_CONCURRENT_SAP_UI_SESSIONS == 3
        assert isinstance(pc.sap_ui_semaphore, threading.Semaphore)

    def test_only_three_run_concurrently(self):
        import playwright_concurrency as pc
        peak = {"n": 0}
        cur = {"n": 0}
        lock = threading.Lock()
        phases = []

        def worker(i):
            acquired = pc.sap_ui_semaphore.acquire(timeout=30)
            assert acquired, "semaphore acquire timed out"
            with lock:
                cur["n"] += 1
                peak["n"] = max(peak["n"], cur["n"])
                phases.append(i)
            time.sleep(0.6)
            with lock:
                cur["n"] -= 1
            pc.sap_ui_semaphore.release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
        assert peak["n"] <= 3, f"more than 3 concurrent SAP-UI slots ({peak['n']})"
        assert peak["n"] == 3, f"expected full 3 slots to be used, peak={peak['n']}"
        assert len(phases) == 6
        # semaphore fully released back to 3 free slots
        got = [pc.sap_ui_semaphore.acquire(blocking=False) for _ in range(3)]
        assert all(got), "semaphore leaked a slot"
        for _ in range(3):
            pc.sap_ui_semaphore.release()

    def test_both_services_acquire_semaphore_and_release_in_finally(self):
        for f in ("/app/backend/sap_playwright_pgr_service.py", "/app/backend/sap_playwright_outbound_gi_service.py"):
            src = open(f).read()
            assert "sap_ui_semaphore.acquire" in src, f"{f} does not acquire the concurrency semaphore"
            assert "sap_ui_semaphore.release" in src, f"{f} does not release the concurrency semaphore"
            acq = src.index("sap_ui_semaphore.acquire")
            rel = src.index("sap_ui_semaphore.release")
            assert "finally:" in src[acq:rel], f"{f} release is not inside a finally block (slot leak on error)"


# ---------- module: stock_transfer_service._build_line_status ----------
class TestBuildLineStatus:
    @pytest.fixture(scope="class")
    def fn(self):
        import stock_transfer_service as sts
        return sts._build_line_status

    DOC = {"items": [
        {"line_no": 1, "product_id": "P1"},
        {"line_no": 2, "product_id": "P2"},
        {"line_no": 3, "product_id": "P3"},
    ]}

    def test_shipped_from_sap_fulfilment_code(self, fn):
        out = fn(self.DOC, [{"product_id": "P1", "order_fulfilment_status": "3"},
                            {"product_id": "P2", "order_fulfilment_status": "1"}])
        by = {r["product_id"]: r for r in out}
        assert len(out) == 3
        assert by["P1"]["status"] == "shipped"
        assert by["P2"]["status"] == "pending"
        assert by["P3"]["status"] == "pending"
        assert [r["line_no"] for r in out] == [1, 2, 3]

    def test_failed_and_insufficient_take_precedence(self, fn):
        out = fn(self.DOC, [{"product_id": "P1", "order_fulfilment_status": "3"}],
                 insufficient_products={"P2": "needed 5, available 1"},
                 failed_products={"P1": "SAP rejected"})
        by = {r["product_id"]: r for r in out}
        assert by["P1"]["status"] == "failed" and by["P1"]["note"] == "SAP rejected"
        assert by["P2"]["status"] == "insufficient_stock" and by["P2"]["note"]
        assert by["P3"]["status"] == "pending"

    def test_force_shipped(self, fn):
        out = fn(self.DOC, [], force_shipped={"P3"})
        by = {r["product_id"]: r for r in out}
        assert by["P3"]["status"] == "shipped"
        assert by["P1"]["status"] == "pending"

    def test_status_values_match_frontend_style_keys(self, fn):
        allowed = {"shipped", "failed", "insufficient_stock", "pending"}
        out = fn(self.DOC, [{"product_id": "P1", "order_fulfilment_status": "3"}],
                 insufficient_products={"P2": "x"}, failed_products={"P3": "y"})
        assert {r["status"] for r in out} <= allowed


# ---------- module: stock transfer orders read path (gi_line_status) ----------
class TestStockTransferGiLineStatus:
    def test_list_orders_shape(self, api):
        r = api.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120)
        assert r.status_code == 200, r.text[:300]
        orders = r.json()
        assert isinstance(orders, list) and orders
        assert all("_id" not in o for o in orders), "mongo _id leaked"
        assert "gi_status" in orders[0] and "items" in orders[0]

    def test_get_single_order_and_line_status_contract(self, api):
        orders = api.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120).json()
        target = orders[0]
        r = api.get(f"{BASE_URL}/api/stock-transfer/orders/{target['sto_id']}", timeout=120)
        assert r.status_code == 200, r.text[:300]
        doc = r.json()
        assert doc["sto_id"] == target["sto_id"]
        assert "_id" not in doc
        lines = doc.get("gi_line_status")
        if lines is None:
            # legacy order (posted before this feature) - UI falls back to gi_status
            assert doc.get("gi_status") is not None
            pytest.skip(f"{target['sto_id']} predates gi_line_status (fallback path used by UI)")
        assert isinstance(lines, list) and len(lines) == len(doc["items"])
        for l in lines:
            assert {"line_no", "product_id", "status"} <= set(l)
            assert l["status"] in {"shipped", "failed", "insufficient_stock", "pending"}

    def test_any_order_with_line_status_is_consistent(self, api):
        orders = api.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120).json()
        withls = [o for o in orders if o.get("gi_line_status")]
        if not withls:
            pytest.skip("No STO with gi_line_status yet (all orders predate the feature)")
        for o in withls:
            assert len(o["gi_line_status"]) == len(o["items"]), o["sto_id"]
            if o.get("gi_status") == "posted":
                assert all(l["status"] == "shipped" for l in o["gi_line_status"]), o["sto_id"]


# ---------- wording rule: no SAP/browser/login leakage in job-status UI strings ----------
class TestNoAutomationWordingInBadges:
    def test_badge_and_progress_strings_are_generic(self):
        src = open("/app/frontend/src/pages/InboundReceiptsPage.js").read()
        start = src.index("const etaText")
        end = src.index("export default function")
        helpers = src[start:end]
        for banned in ("SAP", "login", "browser", "navigat", "Playwright", "Chromium"):
            # docstring/comment lines are excluded - only user-visible strings matter
            code_lines = [l for l in helpers.splitlines() if not l.strip().startswith("//")]
            assert banned.lower() not in "\n".join(code_lines).lower(), f"'{banned}' leaked into badge/eta helpers"

    def test_progress_panel_string_is_generic(self):
        src = open("/app/frontend/src/pages/InboundReceiptsPage.js").read()
        line = [l for l in src.splitlines() if "inbound-receipt-progress-step" in l]
        assert line, "progress step element missing"
        idx = src.splitlines().index(line[0])
        block = "\n".join(src.splitlines()[idx:idx + 4])
        for banned in ("SAP", "browser", "Playwright", "login"):
            assert banned.lower() not in block.lower(), f"'{banned}' leaked into progress panel"
