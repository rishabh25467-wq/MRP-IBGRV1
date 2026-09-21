"""
Iteration 194 - Backend perf-fix verification for Act-as-Supplier PO loading.

All tests are in ONE class so pytest-xdist's `loadscope` pins them to a
single worker - the underlying endpoint hits a live SAP OData with a
shared Semaphore(3), running 2 in parallel starves that limit.

Scope (backend only, per review request):
  1. GET /api/admin/act-as-supplier/{RAD-P2-S}/purchase-orders (336 items)
  2. GET /api/admin/act-as-supplier/{H1330}/purchase-orders (regression)
  3. GET /api/supplier-portal/purchase-orders (H1330 as supplier)
  4. Math sanity (non-negative, no wildly-larger already_shipped_qty)
  5. Oversized ship_qty on POST shipments -> must be rejected 400
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")

ADMIN_SESSION = "iter183actassupplierpos000000000000000000000000"
RAD_P2_S = "b76125a8-06aa-4894-847a-c67d1010eb0b"
H1330 = "62ba27e2-e99a-49c6-9a89-bc804bb50dd5"

SUPPLIER_EMAIL = "test@test.com"
SUPPLIER_PASSWORD = "Test123"

TIMEOUT = 120
PERF_CEILING_S = 45.0  # bug baseline was 49.4s


class TestPOLoadingFix:
    """All-in-one class => pinned to a single xdist worker via loadscope."""

    @pytest.fixture(scope="class")
    def admin_client(self):
        s = requests.Session()
        s.cookies.set("vms_session", ADMIN_SESSION)
        return s

    @pytest.fixture(scope="class")
    def supplier_client(self):
        s = requests.Session()
        r = s.post(
            f"{BASE_URL}/api/supplier-portal/login",
            json={"email": SUPPLIER_EMAIL, "password": SUPPLIER_PASSWORD},
            timeout=30,
        )
        if r.status_code != 200:
            pytest.skip(f"Supplier login failed: {r.status_code} {r.text[:200]}")
        return s

    @pytest.fixture(scope="class")
    def rad_pos(self, admin_client):
        url = f"{BASE_URL}/api/admin/act-as-supplier/{RAD_P2_S}/purchase-orders"
        t0 = time.time()
        r = admin_client.get(url, timeout=TIMEOUT)
        elapsed = time.time() - t0
        print(f"\n[fixture rad_pos] status={r.status_code} elapsed={elapsed:.2f}s")
        assert r.status_code == 200, r.text[:300]
        payload = r.json()
        payload["_elapsed"] = elapsed
        return payload

    def test_1_rad_p2s_shape_and_perf(self, rad_pos):
        elapsed = rad_pos["_elapsed"]
        pos = rad_pos["purchase_orders"]
        print(f"[RAD-P2-S] items={len(pos)} elapsed={elapsed:.2f}s")
        assert len(pos) >= 300, f"Expected ~336 items, got {len(pos)}"
        assert elapsed < PERF_CEILING_S, (
            f"Response time {elapsed:.2f}s exceeds {PERF_CEILING_S}s "
            f"(bug baseline was 49.4s; endpoint dominated by live SAP call)"
        )
        required = {
            "po_number", "item_number", "po_qty",
            "in_transit_qty", "received_qty", "remaining_qty",
            "already_shipped_qty", "buyer_entity_name",
        }
        missing = required - set(pos[0].keys())
        assert not missing, f"Missing fields: {missing}"

    def test_2_rad_p2s_math_sanity(self, rad_pos):
        pos = rad_pos["purchase_orders"]
        bad = []
        for it in pos:
            po_qty = float(it.get("po_qty") or 0)
            in_transit = float(it.get("in_transit_qty") or 0)
            received = float(it.get("received_qty") or 0)
            remaining = float(it.get("remaining_qty") or 0)
            already = float(it.get("already_shipped_qty") or 0)
            if in_transit < 0 or received < 0 or remaining < 0 or already < 0:
                bad.append(("negative", it.get("po_number"), it.get("item_number")))
            if po_qty > 0 and already > po_qty * 5 + 10:
                bad.append(("alr>>po", it.get("po_number"), it.get("item_number"), already, po_qty))
        assert not bad, f"Found {len(bad)} anomalies. First 5: {bad[:5]}"
        print(f"[RAD-P2-S] math sanity OK across {len(pos)} items")

    def test_3_h1330_regression(self, admin_client):
        url = f"{BASE_URL}/api/admin/act-as-supplier/{H1330}/purchase-orders"
        t0 = time.time()
        r = admin_client.get(url, timeout=TIMEOUT)
        elapsed = time.time() - t0
        print(f"\n[H1330 act-as-supplier] status={r.status_code} elapsed={elapsed:.2f}s")
        assert r.status_code == 200, r.text[:300]
        assert elapsed < PERF_CEILING_S
        pos = r.json().get("purchase_orders", [])
        assert isinstance(pos, list)
        for it in pos[:20]:
            assert float(it.get("in_transit_qty") or 0) >= 0
            assert float(it.get("received_qty") or 0) >= 0
            assert float(it.get("remaining_qty") or 0) >= 0

    def test_4_supplier_portal_pos(self):
        s = requests.Session()
        lr = s.post(
            f"{BASE_URL}/api/supplier-portal/login",
            json={"email": SUPPLIER_EMAIL, "password": SUPPLIER_PASSWORD},
            timeout=30,
        )
        if lr.status_code != 200:
            pytest.skip(f"Supplier login failed: {lr.status_code} {lr.text[:200]}")
        print(f"\n[supplier login] cookies={list(s.cookies.keys())}")
        url = f"{BASE_URL}/api/supplier-portal/purchase-orders"
        t0 = time.time()
        r = s.get(url, timeout=TIMEOUT)
        elapsed = time.time() - t0
        print(f"\n[supplier-portal H1330] status={r.status_code} elapsed={elapsed:.2f}s")
        assert r.status_code == 200, r.text[:300]
        assert elapsed < PERF_CEILING_S
        data = r.json()
        assert data.get("vendor_code") == "H1330"
        pos = data.get("purchase_orders", [])
        assert isinstance(pos, list) and len(pos) > 0
        for it in pos[:20]:
            assert float(it.get("in_transit_qty") or 0) >= 0
            assert float(it.get("received_qty") or 0) >= 0
            assert float(it.get("remaining_qty") or 0) >= 0

    def test_5_reject_oversized_ship_qty(self, admin_client, rad_pos):
        pos = rad_pos["purchase_orders"]
        target = next(
            (x for x in pos if float(x.get("remaining_qty") or 0) > 0 and x.get("product_id")),
            None,
        )
        assert target, "No PO line with remaining_qty>0 and product_id found"
        oversized = float(target["remaining_qty"]) * 100 + 1_000_000
        payload = {
            "items": [{
                "po_number": target["po_number"],
                "item_number": target["item_number"],
                "product_id": target.get("product_id"),
                "ship_qty": oversized,
                "unit_of_measure": target.get("unit_of_measure", "EA"),
            }],
            "invoice_number": "TEST_NEG_ONLY",
            "invoice_date": "2026-01-01",
            "vendor_invoice_number": "TEST_NEG_ONLY",
        }
        r = admin_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/{RAD_P2_S}/shipments",
            json=payload,
            timeout=60,
        )
        print(f"\n[oversized-shipment] status={r.status_code} body={r.text[:300]}")
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text[:300]}"
        assert "only" in r.text.lower() and "open" in r.text.lower(), (
            f"Rejection message doesn't mention open-qty limit: {r.text[:300]}"
        )
