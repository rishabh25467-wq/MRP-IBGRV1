"""Backend tests (iteration_117) for three just-implemented features:

1. Server-side by-product/scrap enforcement on
   POST /api/production-confirmation/confirm
2. POST /api/production-confirmation/retry-wip-clearing (failed WIP chip Retry)
3. POST /api/production-confirmation/create-and-release-order/{job_id}/resume
   (recovery of a pipeline_error job from its known SAP Proposal ID)
   + regression that the sfg_shortage failure shape is unchanged.

Synthetic BOM/component/history/job docs (all TEST_QA_* prefixed) are used so
nothing is ever written to the live SAP tenant with real lot data - the
confirm calls that are EXPECTED to pass validation use deliberately fake
SAP UUIDs, so the background job fails harmlessly at the SAP boundary
(the assertion is only that the HTTP endpoint accepted it and returned a
job_id, i.e. validation did not block it).
"""
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing from env and /app/frontend/.env")
API = base_url.rstrip("/") + "/api"
TIMEOUT = 180

NOMASS_PRODUCT = "TEST_QA_NOMASS_117"
MASS_PRODUCT = "TEST_QA_MASS_117"
TEST_LOT = "TEST_QA_LOT_117"


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def mongo_db():
    url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    return MongoClient(url)[name]


@pytest.fixture(scope="session")
def client(mongo_db):
    """Synthetic Entra-ID session (no automatable interactive login)."""
    # unique per xdist worker - a shared user doc got deleted by whichever
    # worker finished first, 401-ing the other worker's still-live session
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    oid = f"TEST-QA-OID-117-{worker}"
    user_id = f"2a94b71e-cd64-4c3d-aede-24eb89ab5fad:{oid}"
    token = "TESTQA" + secrets.token_urlsafe(24)
    mongo_db.auth_users.replace_one(
        {"_id": user_id},
        {"_id": user_id, "tid": user_id.split(":")[0], "oid": oid,
         "email": "qa117.tester@rampgroup.co.in", "name": "QA Tester 117", "role": "super_admin",
         "allowed_pages": [], "created_at": datetime.now(timezone.utc),
         "last_login_at": datetime.now(timezone.utc)},
        upsert=True,
    )
    mongo_db.auth_sessions.insert_one(
        {"_id": token, "user_id": user_id, "expires_at": datetime.now(timezone.utc) + timedelta(hours=4)}
    )
    s = requests.Session()
    s.cookies.set("vms_session", token)
    yield s
    mongo_db.auth_sessions.delete_one({"_id": token})
    mongo_db.auth_users.delete_one({"_id": user_id})


@pytest.fixture(scope="module", autouse=True)
def seed_products(mongo_db):
    """Two synthetic BOMs: one assembly-only (no mass component), one with a
    mass-based RM component (iron ingot family)."""
    mongo_db["bom_node_cache"].replace_one(
        {"_id": NOMASS_PRODUCT},
        {"_id": NOMASS_PRODUCT, "groups": [{"items": [
            {"product_id": "TEST_QA_BAG", "description": "Poly bag", "unit_of_measure": "QUANTITY", "quantity": 1},
        ]}]},
        upsert=True,
    )
    mongo_db["bom_node_cache"].replace_one(
        {"_id": MASS_PRODUCT},
        {"_id": MASS_PRODUCT, "groups": [{"items": [
            {"product_id": "TEST_QA_IRON_ING", "description": "IRON INGOT", "unit_of_measure": "MASS", "quantity": 1.0},
        ]}]},
        upsert=True,
    )
    mongo_db["component_master"].delete_one({"_id": MASS_PRODUCT})
    yield
    mongo_db["bom_node_cache"].delete_many({"_id": {"$in": [NOMASS_PRODUCT, MASS_PRODUCT]}})
    mongo_db["component_master"].delete_one({"_id": MASS_PRODUCT})


def _confirm_payload(product_id, **extra):
    p = {
        "production_lot_id": "TEST_QA_LOT_CONFIRM_117",
        "production_lot_uuid": "00000000-0000-0000-0000-000000000000",
        "confirmation_group_uuid": "00000000-0000-0000-0000-000000000001",
        "reporting_point_uuid": "00000000-0000-0000-0000-000000000002",
        "main_output_product": product_id,
        "confirmed_quantity": 1,
        "unit_code": "EA",
        "confirmation_finished": False,
        "actor": "QA Tester 117",
    }
    p.update(extra)
    return p


# ---------- Feature 1: by-product backend enforcement on /confirm ----------
class TestByProductEnforcement:
    def test_a_assembly_only_item_is_not_blocked(self, client, mongo_db):
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(NOMASS_PRODUCT), timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body.get("job_id"), str) and body["job_id"]
        # job doc really exists (background job created, validation passed)
        st = client.get(f"{API}/production-confirmation/confirm/status/{body['job_id']}", timeout=TIMEOUT)
        assert st.status_code == 200
        assert st.json()["status"] in ("running", "failed", "done")

    def test_b_mass_component_without_net_weight_is_blocked(self, client, mongo_db):
        mongo_db["component_master"].delete_one({"_id": MASS_PRODUCT})
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(MASS_PRODUCT), timeout=TIMEOUT)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "by-product is expected" in detail
        assert "Net Weight" in detail

    def test_c_net_weight_set_but_no_byproduct_target_is_blocked(self, client, mongo_db):
        mongo_db["component_master"].update_one(
            {"_id": MASS_PRODUCT}, {"$set": {"net_weight_kg": 0.4}}, upsert=True)
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(MASS_PRODUCT), timeout=TIMEOUT)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "no Output Products line" in detail

    def test_d_with_byproduct_target_proceeds(self, client, mongo_db):
        mongo_db["component_master"].update_one(
            {"_id": MASS_PRODUCT}, {"$set": {"net_weight_kg": 0.4}}, upsert=True)
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(
                            MASS_PRODUCT,
                            byproduct_material_output_uuid="00000000-0000-0000-0000-0000000000bp",
                            byproduct_confirmed_quantity=0.6,
                            byproduct_unit_code="KGM"),
                        timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("job_id"), str)

    def test_d2_new_byproduct_target_also_proceeds(self, client, mongo_db):
        mongo_db["component_master"].update_one(
            {"_id": MASS_PRODUCT}, {"$set": {"net_weight_kg": 0.4}}, upsert=True)
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(
                            MASS_PRODUCT,
                            new_byproduct_product_id="IRON-SCR",
                            new_byproduct_target_logistics_area_id="P1-SCRAP",
                            new_byproduct_confirmed_quantity=0.6,
                            new_byproduct_unit_code="KGM"),
                        timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("job_id"), str)

    def test_e_material_inputs_override_wins(self, client, mongo_db):
        """When the caller passes this lot's real MaterialInputs and none of
        them are mass-based (KGM), no by-product is expected even for a
        product whose cached BOM has a mass component."""
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(MASS_PRODUCT, material_inputs=[
                            {"product_id": "TEST_QA_BAG", "qty_per_unit": 1, "unit_code": "EA"},
                        ]), timeout=TIMEOUT)
        assert r.status_code == 200, r.text

    def test_f_blank_actor_still_rejected(self, client):
        r = client.post(f"{API}/production-confirmation/confirm",
                        json=_confirm_payload(NOMASS_PRODUCT, actor="  "), timeout=TIMEOUT)
        assert r.status_code == 400
        assert "actor" in r.json()["detail"]


# ---------- Feature 2: retry WIP clearing ----------
class TestRetryWipClearing:
    @pytest.fixture(autouse=True)
    def seed_history(self, mongo_db):
        mongo_db["production_confirmation_history"].delete_many({"production_lot_id": TEST_LOT})
        mongo_db["production_confirmation_history"].insert_one({
            "actor": "QA Tester 117", "production_lot_id": TEST_LOT,
            "main_output_product": MASS_PRODUCT, "confirmed_quantity": 1,
            "success": True, "logs": [], "confirmation_finished": True,
            "wip_clearing": {"success": False, "log": "seeded failure"},
            "at": datetime.now(timezone.utc),
        })
        yield
        mongo_db["production_confirmation_history"].delete_many({"production_lot_id": TEST_LOT})

    def test_retry_updates_history_in_place(self, client, mongo_db):
        r = client.post(f"{API}/production-confirmation/retry-wip-clearing",
                        json={"production_lot_id": TEST_LOT, "site_id": "P1", "actor": "QA Tester 117"},
                        timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "wip_clearing" in body
        assert isinstance(body["wip_clearing"].get("success"), bool)
        doc = mongo_db["production_confirmation_history"].find_one({"production_lot_id": TEST_LOT})
        assert doc["wip_clearing"] != {"success": False, "log": "seeded failure"}
        assert doc["wip_clearing"].get("success") == body["wip_clearing"].get("success")
        # latest-batch endpoint (what the UI chip reads) reflects the new outcome
        b = client.post(f"{API}/production-confirmation/history/latest-batch",
                        json={"production_lot_ids": [TEST_LOT]}, timeout=TIMEOUT)
        assert b.status_code == 200
        assert b.json()[TEST_LOT]["wip_clearing"].get("success") == body["wip_clearing"].get("success")

    def test_retry_unknown_lot_returns_404(self, client):
        r = client.post(f"{API}/production-confirmation/retry-wip-clearing",
                        json={"production_lot_id": "TEST_QA_NO_SUCH_LOT_117", "site_id": "P1", "actor": "QA"},
                        timeout=TIMEOUT)
        assert r.status_code == 404, r.text
        assert "No confirmation history" in r.json()["detail"]

    def test_retry_blank_actor_returns_400(self, client):
        r = client.post(f"{API}/production-confirmation/retry-wip-clearing",
                        json={"production_lot_id": TEST_LOT, "site_id": "P1", "actor": " "}, timeout=TIMEOUT)
        assert r.status_code == 400

    def test_retry_missing_field_returns_422(self, client):
        r = client.post(f"{API}/production-confirmation/retry-wip-clearing",
                        json={"production_lot_id": TEST_LOT, "actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code == 422


# ---------- Feature 3: resume failed create-and-release job ----------
class TestResumeFailedOrder:
    JOBS = []

    def _seed_job(self, mongo_db, result, payload_snapshot=True, status="failed"):
        job_id = "TEST_QA_JOB_117_" + secrets.token_hex(6)
        doc = {
            "_id": job_id, "created_at": datetime.now(timezone.utc), "status": status,
            "error": "SAP release step blew up: simulated pipeline exception",
            "result": result,
        }
        if payload_snapshot:
            doc["payload_snapshot"] = {
                "material_id": MASS_PRODUCT, "site_id": "P1", "quantity": 10,
                "unit_code": "EA", "actor": "QA Tester 117",
            }
        mongo_db["background_jobs"].insert_one(doc)
        self.JOBS.append(job_id)
        return job_id

    @pytest.fixture(autouse=True)
    def cleanup(self, mongo_db):
        yield
        if self.JOBS:
            mongo_db["background_jobs"].delete_many({"_id": {"$in": self.JOBS}})
            self.JOBS.clear()

    def test_resume_returns_new_job_that_starts_pipeline(self, client, mongo_db):
        job_id = self._seed_job(mongo_db, {
            "reason": "pipeline_error", "production_proposal_id": "TEST_QA_PROP_117", "production_order_id": None})
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": "QA Tester 117"}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        new_job_id = body.get("job_id")
        assert isinstance(new_job_id, str) and new_job_id and new_job_id != job_id
        assert body.get("production_proposal_id") == "TEST_QA_PROP_117"
        self.JOBS.append(new_job_id)

        seen = []
        for _ in range(15):
            time.sleep(2)
            st = client.get(f"{API}/production-confirmation/create-and-release-order/status/{new_job_id}",
                            timeout=TIMEOUT)
            assert st.status_code == 200
            j = st.json()
            seen.append(j["status"])
            if j["status"] == "waiting_for_order":
                assert j.get("production_proposal_id") == "TEST_QA_PROP_117"
                break
        assert "waiting_for_order" in seen, f"statuses seen: {seen}"
        # Stop the resumed job so it doesn't keep hammering SAP forever
        client.post(f"{API}/production-confirmation/create-and-release-order/{new_job_id}/cancel", timeout=TIMEOUT)

    def test_resume_without_proposal_id_returns_400(self, client, mongo_db):
        job_id = self._seed_job(mongo_db, {
            "reason": "pipeline_error", "production_proposal_id": None, "production_order_id": None})
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code == 400, r.text
        assert "nothing to resume" in r.json()["detail"]

    def test_resume_without_payload_snapshot_returns_400(self, client, mongo_db):
        job_id = self._seed_job(mongo_db, {
            "reason": "pipeline_error", "production_proposal_id": "TEST_QA_PROP_117"}, payload_snapshot=False)
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code == 400, r.text

    def test_resume_unknown_job_returns_404(self, client):
        r = client.post(f"{API}/production-confirmation/create-and-release-order/TEST_QA_NOPE_117/resume",
                        json={"actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code == 404, r.text

    def test_resume_blank_actor_returns_400(self, client, mongo_db):
        job_id = self._seed_job(mongo_db, {
            "reason": "pipeline_error", "production_proposal_id": "TEST_QA_PROP_117"})
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": " "}, timeout=TIMEOUT)
        assert r.status_code == 400
        assert "actor" in r.json()["detail"]

    # regression: sfg_shortage failure shape unchanged
    def test_sfg_shortage_failure_shape_unchanged(self, client, mongo_db):
        job_id = self._seed_job(mongo_db, {
            "reason": "sfg_shortage", "site_id": "P1",
            "short_components": [{"product_id": "SFG1", "description": "Sub Assy",
                                  "unit_of_measure": "EA", "required_qty": 10, "available_qty": 2}]})
        st = client.get(f"{API}/production-confirmation/create-and-release-order/status/{job_id}", timeout=TIMEOUT)
        assert st.status_code == 200
        j = st.json()
        assert j["status"] == "failed"
        assert j["result"]["reason"] == "sfg_shortage"
        assert j["result"]["short_components"][0]["required_qty"] == 10
        assert "production_proposal_id" not in j["result"]
        # resume must NOT be usable for an sfg_shortage job (no proposal exists)
        r = client.post(f"{API}/production-confirmation/create-and-release-order/{job_id}/resume",
                        json={"actor": "QA"}, timeout=TIMEOUT)
        assert r.status_code == 400
