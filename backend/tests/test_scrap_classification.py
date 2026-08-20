"""Iteration 86: scrap-calc RM-classification rules (_pick_rm_item /
_classify_scrap_family) + Production Confirmation regression smoke."""
import os
import subprocess
import sys

import pytest
import requests
from dotenv import dotenv_values

sys.path.insert(0, "/app/backend")

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env["REACT_APP_BACKEND_URL"]).rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def session_token():
    out = subprocess.run(
        ["python", "/app/backend/tests/_setup_test_session.py"],
        capture_output=True, text=True, check=True, cwd="/app/backend", env={**os.environ},
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
    return s


# --- scrap-calc: classification of the picked raw material ---
class TestScrapCalcClassification:
    def test_maz_iron_scrap_family(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/MAZ42117272-TA", timeout=180)
        assert r.status_code == 200, r.text[:400]
        d = r.json()
        assert d["available"] is True, d
        assert d["rm_product_id"] == "HRCOIL1.9X80.5", d
        assert d["rm_description"] == "HR COIL 1.9mm X 80.5mm", d
        assert d["gross_weight_kg"] == pytest.approx(0.49), d
        assert d["net_weight_kg"] == pytest.approx(0.19), d
        assert d["scrap_per_unit_kg"] == pytest.approx(0.3), d
        assert d["scrap_family"] == {"family": "Iron Scrap", "expected_byproduct_code": "IRON-SCR"}, d
        assert "_id" not in d

    def test_zinc_never_picked_as_rm(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/632163-1", timeout=180)
        assert r.status_code == 200, r.text[:400]
        d = r.json()
        assert d["available"] is False, d
        reason = d["reason"].lower()
        assert "raw-material" in reason or "mass-based" in reason, d
        # Zinc must not leak through as the RM anywhere in the payload
        assert "ZINC" not in str(d).upper(), d

    def test_unknown_product_graceful(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/TEST_NO_SUCH_PRODUCT_86", timeout=120)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["available"] is False

    def test_requires_session(self):
        r = requests.get(f"{API}/production-confirmation/scrap-calc/MAZ42117272-TA", timeout=120)
        assert r.status_code == 401, r.text[:200]


# --- unit-level checks of the classification helpers ---
class TestClassificationHelpers:
    @pytest.mark.parametrize("pid,desc,expected", [
        ("HRCOIL1.9X80.5", "HR COIL 1.9mm X 80.5mm", ("Iron Scrap", "IRON-SCR")),
        ("CDA-260", "Brass strip", ("Brass Scrap", "BRASS-SCR")),
        ("COPPER-ROD", "Copper rod 8mm", ("Copper Scrap", "COPPSC")),
        ("ALUMINIUM-SHT", "Aluminium sheet", ("Aluminium Scrap", "ALU-SCRAP")),
        ("SS304-COIL", "Stainless steel coil", ("SS Scrap", "SSSCRAP")),
        ("CRCOIL0.8", "CR Coil 0.8mm", ("CR Iron Scrap", "CR-SCRAP")),
        ("STEELFLAT25", "Steel flat 25mm", ("Iron Scrap", "IRON-SCR")),
    ])
    def test_family_rules(self, pid, desc, expected):
        from server import _classify_scrap_family
        res = _classify_scrap_family(pid, desc)
        assert res is not None, (pid, desc)
        assert (res["family"], res["expected_byproduct_code"]) == expected, (pid, desc, res)

    def test_unclassifiable_returns_none(self):
        from server import _classify_scrap_family
        assert _classify_scrap_family("MAT", "Paint consumable") is None

    def test_pick_rm_excludes_zinc_even_when_largest(self):
        from server import _pick_rm_item
        items = [
            {"product_id": "ZINC-ING", "description": "Zinc Ingot", "quantity": 5.0},
            {"product_id": "HRCOIL2", "description": "HR Coil", "quantity": 0.4},
        ]
        assert _pick_rm_item(items)["product_id"] == "HRCOIL2"

    def test_pick_rm_returns_none_when_only_zinc(self):
        from server import _pick_rm_item
        assert _pick_rm_item([{"product_id": "ZINC-ING", "description": "Zinc Ingot", "quantity": 5.0}]) is None

    def test_pick_rm_prefers_classified_over_larger_unclassified(self):
        from server import _pick_rm_item
        items = [
            {"product_id": "MAT", "description": "Paint", "quantity": 9.0},
            {"product_id": "HRCOIL2", "description": "HR Coil", "quantity": 0.4},
        ]
        assert _pick_rm_item(items)["product_id"] == "HRCOIL2"

    def test_pick_rm_falls_back_to_largest_unclassified(self):
        from server import _pick_rm_item
        items = [
            {"product_id": "MAT", "description": "Paint", "quantity": 0.01},
            {"product_id": "XYZ", "description": "Mystery", "quantity": 2.0},
        ]
        assert _pick_rm_item(items)["product_id"] == "XYZ"


# --- Source of Supply options regression ---
class TestSourceOfSupply:
    def test_htbs_spain_two_combos(self, client):
        r = client.get(f"{API}/production-confirmation/source-of-supply-options/HTBS-SPAIN", timeout=240)
        assert r.status_code == 200, r.text[:400]
        opts = r.json()["options"]
        assert len(opts) == 2, opts
        assert [(o["production_model_id"], o["site_id"]) for o in opts] == [
            ("HTBS-SPAIN_1", "P2"), ("HTBS-SPAIN_2", "P3")], opts
        assert all(o.get("description") for o in opts), opts


# --- rest of the page (light smoke) ---
class TestPageSmoke:
    def test_open_lots(self, client):
        r = client.get(f"{API}/production-confirmation/open-lots", timeout=300)
        assert r.status_code == 200, r.text[:300]

    def test_deviation_reasons(self, client):
        r = client.get(f"{API}/production-confirmation/deviation-reasons", timeout=120)
        assert r.status_code == 200, r.text[:300]

    def test_proposal_history(self, client):
        r = client.get(f"{API}/production-confirmation/proposal-history", timeout=120)
        assert r.status_code == 200, r.text[:300]
