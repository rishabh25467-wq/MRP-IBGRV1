"""Inbound STO Receipt feature (Aug 2026) - /api/inbound-receipts/* endpoints.
Covers auth gating, sites/pending listings, job start + polling contract,
and error handling. Does NOT trigger a real SAP Playwright receive by
default (that takes 40-90s per delivery line and writes to live SAP).
"""
import os
import uuid

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

SESSION_COOKIE = "d03966d2f366b13718f5cb3c914ac03b930792998e786be6"


@pytest.fixture(scope="module")
def anon():
    s = requests.Session()
    return s


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    return s


# ---- auth gating ----
class TestAuthGating:
    @pytest.mark.parametrize("path", [
        "/api/inbound-receipts/sites",
        "/api/inbound-receipts/pending",
        "/api/inbound-receipts/receive-status/does-not-exist",
    ])
    def test_requires_session(self, anon, path):
        r = anon.get(f"{BASE_URL}{path}", timeout=60)
        assert r.status_code == 401, f"{path} -> {r.status_code}"

    def test_receive_requires_session(self, anon):
        r = anon.post(f"{BASE_URL}/api/inbound-receipts/STO-000054/receive", json={"items": []}, timeout=60)
        assert r.status_code == 401


# ---- sites ----
class TestSites:
    def test_sites_list(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/sites", timeout=120)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data.get("sites"), list)
        assert all(isinstance(s, str) and s for s in data["sites"])
        assert "_id" not in str(data)


# ---- pending ----
class TestPending:
    def test_pending_shape(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=300)
        assert r.status_code == 200
        orders = r.json().get("orders")
        assert isinstance(orders, list)
        assert len(orders) > 0, "No pending STOs available - cannot validate shape"
        for o in orders:
            for k in ("sto_id", "sap_order_id", "ship_from_site_id", "ship_to_site_id",
                      "receipt_status", "outbound_delivery_ids", "items"):
                assert k in o, f"missing {k} in {o.get('sto_id')}"
            assert isinstance(o["outbound_delivery_ids"], list) and o["outbound_delivery_ids"]
            assert isinstance(o["items"], list) and o["items"]
            for it in o["items"]:
                assert set(["line_no", "product_id", "description", "unit_of_measure", "requested_qty"]) <= set(it)
            assert o["receipt_status"] != "received"
        assert '"_id"' not in r.text

    def test_pending_site_filter(self, client):
        sites = client.get(f"{BASE_URL}/api/inbound-receipts/sites", timeout=120).json()["sites"]
        assert sites, "no sites to filter by"
        site = sites[0]
        r = client.get(f"{BASE_URL}/api/inbound-receipts/pending", params={"site_id": site}, timeout=300)
        assert r.status_code == 200
        orders = r.json()["orders"]
        assert all(o["ship_to_site_id"] == site for o in orders)

    def test_pending_unknown_site_returns_empty(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/pending", params={"site_id": "ZZZNOSITE"}, timeout=120)
        assert r.status_code == 200
        assert r.json()["orders"] == []


# ---- job status polling contract ----
class TestJobStatus:
    def test_unknown_job_id_404(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/receive-status/{uuid.uuid4()}", timeout=60)
        assert r.status_code == 404
        assert "detail" in r.json()


# ---- receive validation (no live SAP write) ----
class TestReceiveValidation:
    def test_unknown_sto_400(self, client):
        r = client.post(f"{BASE_URL}/api/inbound-receipts/STO-DOESNOTEXIST/receive",
                        json={"items": []}, timeout=120)
        assert r.status_code == 400, r.text
        assert "not found" in r.json()["detail"].lower()

    def test_already_received_short_circuits_without_starting_a_job(self, client):
        """STO-000045 was fully received (live) during iteration 130 -
        re-receiving must NOT start a new Playwright job."""
        r = client.post(f"{BASE_URL}/api/inbound-receipts/STO-000045/receive",
                        json={"items": [{"line_no": 1, "received_qty": 1}]}, timeout=120)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("already_received") is True
        assert "job_id" not in data
        assert data["result"]["status"] == "received"
        assert data["result"]["results"][0]["delivery_id"] == "P8D1-182"

    def test_received_sto_no_longer_pending(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=300)
        ids = [o["sto_id"] for o in r.json()["orders"]]
        assert "STO-000045" not in ids
        assert "STO-000035" not in ids

    def test_bad_payload_422(self, client):
        r = client.post(f"{BASE_URL}/api/inbound-receipts/STO-000054/receive",
                        json={"items": [{"line_no": "abc", "received_qty": "xyz"}]}, timeout=120)
        assert r.status_code == 422, r.text
