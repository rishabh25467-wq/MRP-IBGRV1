"""Iter 194 - regression coverage for the new automated STO GR flow.

Focus:
  * webhook auth (401 vs 200)
  * GET /api/inbound-receipts/pending, /completed, /sites (auth required)
  * receive-status/{job_id} 404 path
  * live "safe" receive for STO-000063 (fast path: SAP already Finished
    -> should immediately relocate {SITE}-HOLD -> P1-RM and return a
    terminal (done/failed/partial) job status quickly, no long polling)

DOES NOT trigger any brand-new SAP write on any other STO.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
SESSION_TOKEN = os.environ.get("ITER194_SESSION_TOKEN", "iter194testinbrec6b8924d4bb6432e52ae1bb2d")
COOKIE = {"vms_session": SESSION_TOKEN}


# ---------- webhook auth ----------
class TestWebhookAuth:
    def test_webhook_rejects_no_auth(self):
        r = requests.post(f"{BASE_URL}/api/webhooks/sap-put-away", json={"type": "x"}, timeout=15)
        assert r.status_code == 401

    def test_webhook_accepts_valid_basic_auth_and_ignores_unrelated_event(self):
        # correct basic-auth creds (from backend/.env) but a payload that
        # doesn't match SiteLogisticsLot -> handler should log + 200 without
        # touching any STO
        user = os.environ.get("SAP_WEBHOOK_USERNAME") or "sap_bydesign"
        pwd = os.environ.get("SAP_WEBHOOK_PASSWORD") or "v-TYA5VXvE8mgdjPoH4StRYOILJBmbWh"
        r = requests.post(
            f"{BASE_URL}/api/webhooks/sap-put-away",
            json={"type": "sap.byd.OtherObject.Root.Created.v1", "data": {"entity-id": "IGNORED"}},
            auth=(user, pwd), timeout=15,
        )
        assert r.status_code == 200
        assert r.json().get("status") == "received"


# ---------- pending / completed / sites ----------
class TestReadEndpoints:
    def test_pending_list(self):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/pending", cookies=COOKIE, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "orders" in data and isinstance(data["orders"], list)

    def test_completed_list(self):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/completed", cookies=COOKIE, timeout=30)
        assert r.status_code == 200, r.text
        assert "orders" in r.json()

    def test_sites_list(self):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/sites", cookies=COOKIE, timeout=30)
        assert r.status_code == 200, r.text
        assert "sites" in r.json()

    def test_completed_bad_date(self):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/completed?date_from=not-a-date", cookies=COOKIE, timeout=30)
        assert r.status_code == 400

    def test_job_status_unknown_id(self):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/receive-status/does-not-exist", cookies=COOKIE, timeout=15)
        assert r.status_code == 404


# ---------- live Receive fast-path on STO-000063 ----------
class TestReceiveSto63FastPath:
    def test_receive_sto_000063_completes_fast_path(self):
        """STO-000063 (P8D1-201) is confirmed Released/Finished in SAP.
        start_automated_receipt should NOT park it as awaiting_sap (no
        Warehouse Order creation expected) - it should relocate stock
        from P1-HOLD -> P1-RM synchronously and return done/partial/failed
        (never stuck 'awaiting_sap')."""
        r = requests.post(
            f"{BASE_URL}/api/inbound-receipts/STO-000063/receive",
            json={"items": []},
            cookies=COOKIE, timeout=60,
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        # Already-received short-circuit is also acceptable
        if payload.get("already_received"):
            assert payload["result"]["status"] == "received"
            return
        job_id = payload.get("job_id")
        assert job_id, payload

        # Poll receive-status. Fast path should terminate within ~60s.
        terminal = {"done", "failed"}
        end = time.time() + 90
        last = None
        while time.time() < end:
            s = requests.get(f"{BASE_URL}/api/inbound-receipts/receive-status/{job_id}", cookies=COOKIE, timeout=15)
            assert s.status_code == 200
            last = s.json()
            if last.get("status") in terminal:
                break
            time.sleep(3)
        assert last is not None
        # Explicitly assert we did NOT get stuck in awaiting_sap on a
        # known-Finished delivery.
        assert last.get("phase") != "awaiting_sap", f"Unexpected awaiting_sap for finished SAP delivery: {last}"
        assert last.get("status") in terminal, f"Job did not terminate: {last}"

        # Fetch STO doc via /pending or /completed to see final receipt_status
        c = requests.get(f"{BASE_URL}/api/inbound-receipts/completed", cookies=COOKIE, timeout=30)
        found = next((o for o in c.json()["orders"] if o["sto_id"] == "STO-000063"), None)
        # If the receipt succeeded/partially/failed it should appear in Completed.
        assert found is not None, "STO-000063 missing from Completed tab after receive"
        assert found["receipt_status"] in ("received", "partial", "failed")
