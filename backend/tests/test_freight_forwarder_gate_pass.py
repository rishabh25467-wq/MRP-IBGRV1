"""Backend tests: mandatory Freight Forwarder on STO creation + Delivery Note /
Gate Pass data (dynamic company letterhead, serial number, freight forwarder).

Aug 2026 feature. Uses the synthetic Entra session helper
tests/_setup_sto_session.py (cookie `vms_session`).

Read-only: does NOT create new STOs (creation writes live to SAP+ERP).
Validation cases only exercise rejected payloads.
"""
import os
import subprocess
from datetime import date, timedelta

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
API = f"{base_url.rstrip('/')}/api"

RAY_STO = "STO-000041"        # P1 -> P8, sale_no 38094, sale_noc 746, freight_forwarder=None
RAY_STO_WITH_FF = "STO-000042"  # P1 -> P8, freight_forwarder='pooja transport'
RADISH_STO = "STO-000040"     # P3 -> P2 (Radish Technologies sites)
RAY_SITES = {"P1", "P5", "P8", "P9"}
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture(scope="module")
def session_token():
    out = subprocess.run(["python", "tests/_setup_sto_session.py"], cwd="/app/backend",
                         capture_output=True, text=True)
    token = out.stdout.strip().splitlines()[-1]
    assert token.startswith("TEST_sto_session_token"), out.stdout + out.stderr
    yield token
    subprocess.run(["python", "tests/_setup_sto_session.py", "--cleanup"], cwd="/app/backend",
                   capture_output=True, text=True)


@pytest.fixture(scope="module")
def client(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    s.headers.update({"Content-Type": "application/json"})
    return s


def _payload(**over):
    p = {
        "ship_from_site_id": "P1",
        "ship_to_site_id": "P8",
        "ship_to_location_id": "P8-SFG",
        "items": [{"product_id": "1124-274", "requested_qty": 1}],
        "transportation_mode": "Road",
        "vehicle_no": "TESTQA0001",
        "place_of_supply": "Uttar Pradesh",
        "gr_no": "GRQA001",
        "date_of_supply": TOMORROW,
        "freight_forwarder": "TEST_FF Transport",
    }
    p.update(over)
    return p


# --- Module: server.py StockTransferOrderCreate / stock_transfer_service validation ---
class TestFreightForwarderValidation:
    def test_missing_freight_forwarder_key_returns_422(self, client):
        p = _payload()
        p.pop("freight_forwarder")
        r = client.post(f"{API}/stock-transfer/orders", json=p)
        assert r.status_code == 422, r.text
        body = r.json()
        assert "freight_forwarder" in str(body), body

    def test_blank_freight_forwarder_rejected(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=_payload(freight_forwarder="   "))
        assert r.status_code in (400, 422), r.text
        assert "freight" in r.text.lower()

    def test_null_freight_forwarder_rejected(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=_payload(freight_forwarder=None))
        assert r.status_code == 422, r.text


# --- Module: stock_transfer_service.get_delivery_note_data ---
class TestDeliveryNoteData:
    @pytest.mark.parametrize("sto_id", [RAY_STO, RAY_STO_WITH_FF, RADISH_STO])
    def test_delivery_note_company_blocks(self, client, sto_id):
        r = client.get(f"{API}/stock-transfer/{sto_id}/delivery-note")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["sto_id"] == sto_id
        for key in ("ship_from_company", "ship_to_company"):
            comp = d[key]
            assert comp, f"{key} is null - live ERP comp lookup failed"
            assert comp["company_name"], comp
            assert comp["address_line1"], f"{key} has no street address: {comp}"
            assert comp["gstin"] and comp["state_code"], comp
        # Dynamic letterhead: Ray sites -> RAY INTERNATIONAL, others -> RADISH
        name = d["ship_from_company"]["company_name"].upper()
        if d["ship_from_site_id"] in RAY_SITES:
            assert "RAY" in name, name
        else:
            assert "RADISH" in name, name

    def test_serial_number_format(self, client):
        d = client.get(f"{API}/stock-transfer/{RAY_STO}/delivery-note").json()
        assert d["serial_number"], d
        parts = d["serial_number"].split("-")
        assert len(parts) == 3, d["serial_number"]
        assert parts[0] == d["ship_from_site_id"]
        assert parts[1] == str(d["erp_sale_noc"])
        assert parts[2].isdigit() and len(parts[2]) == 4, f"session segment: {parts[2]}"

    def test_freight_forwarder_passthrough(self, client):
        assert client.get(f"{API}/stock-transfer/{RAY_STO}/delivery-note").json()["freight_forwarder"] in (None, "")
        assert client.get(f"{API}/stock-transfer/{RAY_STO_WITH_FF}/delivery-note").json()["freight_forwarder"] == "pooja transport"

    def test_gate_pass_source_fields(self, client):
        """Gate Pass page reuses /delivery-note; needs sale_noc, sale_no, date, vehicle, ship_to company."""
        d = client.get(f"{API}/stock-transfer/{RAY_STO}/delivery-note").json()
        assert d["erp_sale_noc"] == 746
        assert d["erp_sale_no"] == 38094
        assert d["date_of_supply"]
        assert d["vehicle_no"]
        assert "RAY" in d["ship_to_company"]["company_name"].upper()

    def test_unknown_sto_returns_404(self, client):
        r = client.get(f"{API}/stock-transfer/STO-999999/delivery-note")
        assert r.status_code == 404, r.text

    def test_delivery_note_requires_auth(self):
        r = requests.get(f"{API}/stock-transfer/{RAY_STO}/delivery-note")
        assert r.status_code in (401, 403), r.status_code


# --- Module: GET /api/stock-transfer/orders (list shows freight_forwarder) ---
def test_orders_list_exposes_freight_forwarder(client):
    r = client.get(f"{API}/stock-transfer/orders")
    assert r.status_code == 200, r.text
    body = r.json()
    orders = body if isinstance(body, list) else body.get("orders", [])
    assert orders
    match = [o for o in orders if o.get("sto_id") == RAY_STO_WITH_FF or o.get("id") == RAY_STO_WITH_FF]
    assert match, "STO-000042 not in list"
    assert match[0].get("freight_forwarder") == "pooja transport", match[0]
    assert all("_id" not in o for o in orders), "Mongo _id leaked into response"
