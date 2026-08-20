"""Backend tests for the Production Confirmation scrap auto-calc feature
(GET /api/production-confirmation/scrap-calc/{product_id}) plus a smoke
check of the rest of the Production Confirmation endpoints."""
import os
import subprocess

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env["REACT_APP_BACKEND_URL"]).rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def session_token():
    out = subprocess.run(
        ["python", "/app/backend/tests/_setup_test_session.py"],
        capture_output=True, text=True, check=True, cwd="/app/backend",
        env={**os.environ},
    )
    token = out.stdout.strip().splitlines()[-1]
    assert token.startswith("TEST_")
    yield token
    subprocess.run(["python", "/app/backend/tests/_setup_test_session.py", "--cleanup"],
                   capture_output=True, text=True, cwd="/app/backend")


@pytest.fixture(scope="session")
def client(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    s.headers.update({"Content-Type": "application/json"})
    return s


# --- auth gating on the new endpoint ---
class TestScrapCalcAuth:
    def test_requires_session(self):
        r = requests.get(f"{API}/production-confirmation/scrap-calc/MAZ42117272-TA", timeout=120)
        assert r.status_code == 401, r.text[:300]


# --- the new scrap-calc endpoint ---
class TestScrapCalc:
    def test_maz_material_expected_values(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/MAZ42117272-TA", timeout=120)
        assert r.status_code == 200, r.text[:500]
        d = r.json()
        assert d["available"] is True, d
        assert d["rm_product_id"] == "HRCOIL1.9X80.5", d
        assert d["gross_weight_kg"] == pytest.approx(0.49), d
        assert d["net_weight_kg"] == pytest.approx(0.19), d
        assert d["scrap_per_unit_kg"] == pytest.approx(0.3), d
        assert "_id" not in d

    def test_missing_net_weight_reports_unavailable(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/632163-1", timeout=120)
        assert r.status_code == 200, r.text[:500]
        d = r.json()
        assert d["available"] is False, d
        assert "Net Weight" in d["reason"], d
        # partial info still returned so the UI can explain what's missing
        assert d.get("gross_weight_kg") is not None, d

    def test_no_mass_component_reports_unavailable(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/F041811", timeout=120)
        assert r.status_code == 200, r.text[:500]
        d = r.json()
        assert d["available"] is False, d
        assert "mass-based" in d["reason"].lower() or "net weight" in d["reason"].lower(), d

    def test_unknown_product_graceful(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/TEST_NO_SUCH_PRODUCT_123", timeout=120)
        assert r.status_code == 200, r.text[:500]
        d = r.json()
        assert d["available"] is False
        assert d["reason"]

    def test_product_id_with_slash_or_space_no_500(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/{requests.utils.quote('A B/C', safe='')}", timeout=120)
        assert r.status_code in (200, 404), r.text[:300]


# --- regression smoke of the rest of the page's endpoints ---
class TestProductionConfirmationSmoke:
    def test_open_lots(self, client):
        r = client.get(f"{API}/production-confirmation/open-lots", timeout=240)
        assert r.status_code == 200, r.text[:500]
        body = r.json()
        rows = body if isinstance(body, list) else body.get("lots", body.get("rows", []))
        assert isinstance(rows, list)
        for row in rows[:5]:
            assert "_id" not in row

    def test_deviation_reasons(self, client):
        r = client.get(f"{API}/production-confirmation/deviation-reasons", timeout=120)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        items = body if isinstance(body, list) else body.get("reasons", [])
        assert isinstance(items, list)

    def test_history(self, client):
        r = client.get(f"{API}/production-confirmation/history", timeout=120)
        assert r.status_code == 200, r.text[:300]

    def test_source_of_supply_options(self, client):
        r = client.get(f"{API}/production-confirmation/source-of-supply-options/MAZ42117272-TA", timeout=240)
        assert r.status_code == 200, r.text[:300]
