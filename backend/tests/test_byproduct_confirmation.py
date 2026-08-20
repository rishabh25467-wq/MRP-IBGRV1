"""Backend tests for the by-product (scrap) confirmation flow on the
Production Confirmation page (iteration_87).

Covers:
  - auth gate on /api/production-confirmation/*
  - GET /production-confirmation/open-lots  -> material_outputs schema
  - GET /production-confirmation/lot/{id}   -> material_outputs schema
  - GET /production-confirmation/scrap-calc/{product_id} -> scrap_family
  - POST /production-confirmation/confirm   -> request-model validation
    (byproduct_* fields accepted, no 422) via the OpenAPI schema + payload
    validation errors. The happy path is NOT posted here: it writes real
    data into the live SAP tenant.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing from env and /app/frontend/.env")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"
TIMEOUT = 180


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def mongo_db():
    url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    return MongoClient(url)[name]


@pytest.fixture(scope="session")
def session_token(mongo_db):
    """Synthetic Entra-ID session (app has no automatable interactive login)."""
    user_id = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-QA-OID"
    token = "TESTQA" + secrets.token_urlsafe(24)
    mongo_db.auth_users.replace_one(
        {"_id": user_id},
        {"_id": user_id, "tid": user_id.split(":")[0], "oid": "TEST-QA-OID",
         "email": "qa.tester@rampgroup.co.in", "name": "QA Tester", "role": "super_admin",
         "allowed_pages": [], "created_at": datetime.now(timezone.utc),
         "last_login_at": datetime.now(timezone.utc)},
        upsert=True,
    )
    mongo_db.auth_sessions.insert_one(
        {"_id": token, "user_id": user_id,
         "expires_at": datetime.now(timezone.utc) + timedelta(hours=4)}
    )
    yield token
    mongo_db.auth_sessions.delete_one({"_id": token})
    mongo_db.auth_users.delete_one({"_id": user_id})


@pytest.fixture(scope="session")
def client(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    return s


@pytest.fixture(scope="session")
def open_lots(client):
    r = client.get(f"{API}/production-confirmation/open-lots", params={"limit": 100}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text[:400]
    rows = r.json()["rows"]
    assert isinstance(rows, list) and rows, "expected at least one open production lot"
    return rows


# ---------- auth gate ----------
class TestAuthGate:
    def test_open_lots_requires_session(self):
        r = requests.get(f"{API}/production-confirmation/open-lots", timeout=60)
        assert r.status_code == 401
        assert "Login" in r.json().get("detail", "")

    def test_confirm_requires_session(self):
        r = requests.post(f"{API}/production-confirmation/confirm", json={}, timeout=60)
        assert r.status_code == 401


# ---------- open-lots / material_outputs ----------
class TestOpenLotsMaterialOutputs:
    def test_rows_expose_material_outputs(self, open_lots):
        for row in open_lots:
            assert "material_outputs" in row, f"lot {row['production_lot_id']} missing material_outputs"
            assert isinstance(row["material_outputs"], list)
            assert "_id" not in row

    def test_material_output_line_schema(self, open_lots):
        lines = [mo for r in open_lots for mo in r["material_outputs"]]
        assert lines, "no material_output lines returned at all"
        for mo in lines:
            for key in ("product_id", "material_output_uuid", "unit_code", "planned_quantity", "open_quantity"):
                assert key in mo, f"{key} missing from material_output line {mo}"
            assert isinstance(mo["product_id"], str) and mo["product_id"]
            assert isinstance(mo["material_output_uuid"], str) and len(mo["material_output_uuid"]) >= 32

    def test_main_output_present_in_material_outputs(self, open_lots):
        """Every lot's main output product should also appear as an output line."""
        bad = [r["production_lot_id"] for r in open_lots
               if r.get("main_output_product")
               and r["material_outputs"]
               and r["main_output_product"] not in [m["product_id"] for m in r["material_outputs"]]]
        assert not bad, f"main_output_product not found in material_outputs for lots {bad}"

    def test_some_lot_has_a_byproduct_line(self, open_lots):
        """At least one lot must carry a 2nd (by-product) output line so the
        'found' UI path is reachable."""
        multi = [(r["production_lot_id"], [m["product_id"] for m in r["material_outputs"]])
                 for r in open_lots if len(r["material_outputs"]) > 1]
        assert multi, "no lot with a by-product output line found in the first 100 open lots"

    def test_lot_by_id_matches_open_lots(self, client, open_lots):
        lot_id = next((r["production_lot_id"] for r in open_lots if len(r["material_outputs"]) > 1), None)
        assert lot_id, "no multi-output lot to cross-check"
        r = client.get(f"{API}/production-confirmation/lot/{lot_id}", timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:400]
        rows = r.json()["rows"]
        assert rows and all(x["production_lot_id"] == lot_id for x in rows)
        assert all(isinstance(x.get("material_outputs"), list) for x in rows)

    def test_lot_by_id_unknown_returns_404(self, client):
        r = client.get(f"{API}/production-confirmation/lot/TEST_NO_SUCH_LOT", timeout=TIMEOUT)
        assert r.status_code in (404, 502), r.text[:300]


# ---------- scrap-calc ----------
class TestScrapCalc:
    def test_available_product_returns_scrap_family(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/5989829-1", timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:400]
        d = r.json()
        assert d["available"] is True
        assert d["gross_weight_kg"] > 0 and d["net_weight_kg"] > 0
        assert d["scrap_per_unit_kg"] == pytest.approx(d["gross_weight_kg"] - d["net_weight_kg"], abs=1e-6)
        assert set(d["scrap_family"]) == {"family", "expected_byproduct_code"}
        assert d["scrap_family"]["expected_byproduct_code"]

    def test_unavailable_product_gives_reason(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/PL-0049", timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:400]
        d = r.json()
        assert d["available"] is False
        assert d.get("reason")

    def test_unknown_product(self, client):
        r = client.get(f"{API}/production-confirmation/scrap-calc/TEST_NOPE_999", timeout=TIMEOUT)
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.json()["available"] is False


# ---------- confirm request model ----------
class TestConfirmRequestModel:
    def test_request_model_declares_byproduct_fields(self):
        """OpenAPI is only reachable on the internal port (public ingress only
        routes /api) and importing server.py starts background jobs, so the
        model declaration is asserted statically."""
        src = Path("/app/backend/server.py").read_text(encoding="utf-8")
        model = src.split("class ConfirmProductionRequest(BaseModel):", 1)[1].split("\n\n\n", 1)[0]
        for f in ("confirmed_scrap", "byproduct_material_output_uuid",
                  "byproduct_confirmed_quantity", "byproduct_unit_code"):
            assert f in model, f"{f} missing from ConfirmProductionRequest"

    def test_missing_required_fields_is_422(self, client):
        r = client.post(f"{API}/production-confirmation/confirm",
                        json={"production_lot_id": "1"}, timeout=60)
        assert r.status_code == 422, r.text[:300]

    def test_blank_actor_rejected(self, client, open_lots):
        row = open_lots[0]
        payload = {
            "production_lot_id": row["production_lot_id"],
            "production_lot_uuid": row["production_lot_uuid"],
            "confirmation_group_uuid": row["confirmation_group_uuid"],
            "reporting_point_uuid": row["reporting_point_uuid"],
            "confirmed_quantity": 1,
            "confirmed_scrap": 0.1,
            "byproduct_material_output_uuid": None,
            "byproduct_confirmed_quantity": None,
            "byproduct_unit_code": None,
            "actor": "   ",
        }
        r = client.post(f"{API}/production-confirmation/confirm", json=payload, timeout=60)
        assert r.status_code == 400, r.text[:300]
        assert "actor" in r.json()["detail"]

    def test_bad_byproduct_quantity_type_is_422(self, client, open_lots):
        row = open_lots[0]
        payload = {
            "production_lot_id": row["production_lot_id"],
            "production_lot_uuid": row["production_lot_uuid"],
            "confirmation_group_uuid": row["confirmation_group_uuid"],
            "reporting_point_uuid": row["reporting_point_uuid"],
            "byproduct_confirmed_quantity": "not-a-number",
            "actor": "QA",
        }
        r = client.post(f"{API}/production-confirmation/confirm", json=payload, timeout=60)
        assert r.status_code == 422, r.text[:300]


# ---------- regression: rest of the page's endpoints ----------
class TestPageRegression:
    @pytest.mark.parametrize("path", [
        "/production-confirmation/deviation-reasons",
        "/production-confirmation/history",
        "/production-confirmation/proposal-history",
    ])
    def test_supporting_endpoints_200(self, client, path):
        r = client.get(f"{API}{path}", timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), dict)

    def test_component_availability(self, client, open_lots):
        row = next((r for r in open_lots if r.get("main_output_product")), None)
        r = client.get(f"{API}/production-confirmation/component-availability",
                       params={"main_output_product": row["main_output_product"],
                               "confirmed_quantity": 1, "site_id": row.get("site_id")},
                       timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:300]
        assert "checked" in r.json()
