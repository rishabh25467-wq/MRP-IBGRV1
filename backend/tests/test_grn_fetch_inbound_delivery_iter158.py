"""Iter 158 - manual 'Fetch from SAP' inbound-delivery button.

Covers:
- POST /api/admin/grn/{doc_code}/fetch-inbound-delivery (job start)
- GET  /api/admin/grn/fetch-inbound-delivery/poll/{job_id}   (job poll)
- 404 for unknown doc_code, 404 for unknown job_id
- Positive case: ZTESTIBD1 (PO 29368 + FWIPL/26-27/T555) hits real
  SAP and comes back with inbound_delivery_id='52897', sap_sync_status
  flips to 'posted', per_po entry tagged manually_confirmed=True.
- Negative case: ZTESTIBD2 (same PO but fake bill 'NONEXISTENT-BILL-999')
  polls back status='failed' with a clear error string, shipment
  unchanged.

The positive case genuinely calls SAP - allow up to 3 min of polling.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
COOKIE = {"vms_session": "1_kcTPauhhZHOtN1pg8W3A8lLNhprB2jyZ-jsA4qC5s"}


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.update(COOKIE)
    return s


def _poll(client, job_id, timeout=180):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"{BASE_URL}/api/admin/grn/fetch-inbound-delivery/poll/{job_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("done", "failed"):
            return last
        time.sleep(5)
    pytest.fail(f"Job did not finish in {timeout}s. Last: {last}")


class TestFetchInboundDelivery:
    def test_auth_required(self):
        r = requests.post(f"{BASE_URL}/api/admin/grn/ZTESTIBD1/fetch-inbound-delivery")
        assert r.status_code in (401, 403), r.text

    def test_unknown_doc_returns_404(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/DOESNOTEXIST/fetch-inbound-delivery")
        assert r.status_code == 404

    def test_unknown_job_returns_404(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/fetch-inbound-delivery/poll/does-not-exist")
        assert r.status_code == 404

    def test_positive_fetch_ZTESTIBD1(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/ZTESTIBD1/fetch-inbound-delivery")
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        assert isinstance(job_id, str) and len(job_id) > 10

        final = _poll(client, job_id, timeout=180)
        assert final["status"] == "done", f"Expected done, got {final}"
        assert final["error"] is None

        ship = final["result"]["shipment"]
        assert ship["_id"] == "ZTESTIBD1"
        assert ship["sap_sync_status"] == "posted"
        per_po = ship["sap_gr_result"]["per_po"]
        target = [p for p in per_po if p["po_number"] == "29368"]
        assert len(target) == 1
        entry = target[0]
        assert entry["inbound_delivery_id"] == "52897"
        assert entry["status"] == "posted"
        assert entry["manually_confirmed"] is True
        assert "manually_confirmed_by" in entry
        assert "manually_confirmed_at" in entry

    def test_negative_fetch_ZTESTIBD2_no_match(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/ZTESTIBD2/fetch-inbound-delivery")
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        final = _poll(client, job_id, timeout=180)
        assert final["status"] == "failed", f"Expected failed, got {final}"
        assert final["error"] is not None
        # Error should be human-readable and mention the fake bill number
        assert "NONEXISTENT-BILL-999" in final["error"] or "no matching" in final["error"].lower()

    def test_pending_shipments_list_unaffected(self, client):
        # regression: pending list still renders (endpoint 200)
        r = client.get(f"{BASE_URL}/api/admin/grn/shipments?status=in_transit")
        assert r.status_code == 200
        assert "shipments" in r.json()

    def test_confirmed_shipments_list_unaffected(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/shipments?status=approved")
        assert r.status_code == 200
        ships = r.json()["shipments"]
        assert any(s["_id"] == "ZTESTIBD1" for s in ships)
