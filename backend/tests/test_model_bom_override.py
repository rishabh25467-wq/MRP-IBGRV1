"""Aug 2026 change under test: pre-flight Component Stock Availability for a
NEW production order must use the explicitly-chosen Production Model's REAL
linked BOM (SAPProductionModelBomClient -> productionmodelbomemergent OData)
instead of bom_cache_service's "highest revision wins" guess.

Covers:
  - SAPProductionModelBomClient.get_bill_of_material_id_for_model (live SAP)
  - production_confirmation_service.check_component_availability override path
  - regression: /component-availability (existing-lot tab) unchanged
  - regression: /create-and-release-order still accepts payload WITHOUT
    production_model_uuid (validation-level only, no live SAP order created)
"""
import os
import sys
import pytest
import requests
from dotenv import dotenv_values

sys.path.insert(0, "/app/backend")

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

MATERIAL = "MAZ42117272-TA"
SITE = "P2"
REAL_BOM_ID = "MAZ42117272-TA_1"      # real released model's BOM (2.0mm coil)
CACHED_GUESS_BOM_ID = "MAZ42117272-TA_2"  # highest-revision cached guess (1.9mm)


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def session_token():
    import subprocess
    out = subprocess.run(
        [sys.executable, "/app/backend/tests/_setup_pc_session.py"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="session")
def api(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    return s


@pytest.fixture(scope="session")
def env_cfg():
    return dotenv_values("/app/backend/.env")


@pytest.fixture(scope="session")
def bom_model_client(env_cfg):
    from sap_production_model_client import SAPProductionModelBomClient
    base = env_cfg["SAP_ODATA_PRODUCTION_MODEL_BOM_BASE_URL"]
    return SAPProductionModelBomClient(
        base, env_cfg["SAP_ODATA_USERNAME"], env_cfg["SAP_ODATA_PASSWORD"],
    )


@pytest.fixture(scope="session")
def mongo_db(env_cfg):
    from pymongo import MongoClient
    client = MongoClient(env_cfg["MONGO_URL"])
    return client[env_cfg["DB_NAME"]]


@pytest.fixture(scope="session")
def sos_options(api):
    r = api.get(
        f"{BASE_URL}/api/production-confirmation/source-of-supply-options/{MATERIAL}",
        params={"site_id": SITE}, timeout=120,
    )
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert data.get("material_uuid"), "material_uuid missing - picker would be hidden"
    return data["options"]


# ---------- Source of Supply picker data ----------
class TestSourceOfSupplyOptions:
    def test_multiple_options_with_model_uuid(self, sos_options):
        # NOTE (live SAP, Aug 2026): SAP now returns only ONE valid
        # Production Model for this material (MAZ42117272-TA_1); the _2
        # model no longer surfaces a released source of supply. The picker
        # still renders for a single option.
        assert len(sos_options) >= 1, f"expected >=1 model, got {sos_options}"
        for o in sos_options:
            assert o.get("production_model_uuid"), f"option missing production_model_uuid: {o}"
            assert o.get("logistic_relationship_uuid")
            assert o["site_id"] == SITE
        ids = [o["production_model_id"] for o in sos_options]
        assert REAL_BOM_ID in ids, f"expected {REAL_BOM_ID} among {ids}"


# ---------- NEW: real BOM lookup per model ----------
class TestRealBomForModel:
    def test_correct_model_resolves_to_real_bom(self, sos_options, bom_model_client):
        opt = next(o for o in sos_options if o["production_model_id"] == REAL_BOM_ID)
        bom_id = bom_model_client.get_bill_of_material_id_for_model(opt["production_model_uuid"])
        print(f"model {opt['production_model_id']} -> BOM {bom_id}")
        assert bom_id == REAL_BOM_ID, f"expected real BOM {REAL_BOM_ID}, got {bom_id}"

    def test_each_model_resolves_and_maps_distinctly(self, sos_options, bom_model_client):
        resolved = {}
        for o in sos_options:
            resolved[o["production_model_id"]] = bom_model_client.get_bill_of_material_id_for_model(
                o["production_model_uuid"]
            )
        print(f"model -> BOM map: {resolved}")
        assert all(v for v in resolved.values()), f"some models resolved to no BOM: {resolved}"
        assert len(set(resolved.values())) == len(resolved), f"models collapsed to same BOM: {resolved}"

    def test_unknown_model_uuid_returns_none(self, bom_model_client):
        assert bom_model_client.get_bill_of_material_id_for_model(
            "00000000-0000-0000-0000-000000000000"
        ) is None


# ---------- override wiring in the availability check ----------
class TestAvailabilityOverride:
    def test_cached_default_uses_highest_revision(self, mongo_db):
        import production_confirmation_service as pcs
        cached = mongo_db["bom_node_cache"].find_one({"_id": MATERIAL})
        assert cached, "no cached BOM doc for the test material"
        print(f"cached bom_id = {cached.get('bom_id')}")
        assert cached.get("bom_id") == CACHED_GUESS_BOM_ID
        res = pcs.check_component_availability(mongo_db, MATERIAL, 1, SITE)
        assert res["checked"] is True
        comps = [c["product_id"] for c in res["components"]]
        print(f"default components: {comps}")
        assert any("1.9" in c for c in comps), f"expected the 1.9mm coil in cached default, got {comps}"

    def test_override_uses_chosen_models_real_bom(self, mongo_db, env_cfg):
        import production_confirmation_service as pcs
        from sap_soap_client import SAPSoapBOMClient
        soap = SAPSoapBOMClient(
            env_cfg["SAP_SOAP_ENDPOINT"], env_cfg["SAP_SOAP_USERNAME"], env_cfg["SAP_SOAP_PASSWORD"],
        )
        res = pcs.check_component_availability(
            mongo_db, MATERIAL, 1, SITE, None, REAL_BOM_ID, soap,
        )
        assert res["checked"] is True, res
        comps = [c["product_id"] for c in res["components"]]
        print(f"override components: {comps}")
        assert comps, "override BOM produced zero components"
        assert any("2.0" in c for c in comps), f"expected the 2.0mm coil with override, got {comps}"
        assert not any("1.9" in c for c in comps), f"1.9mm coil should not appear with override, got {comps}"
        for c in res["components"]:
            assert "required_qty" in c and "sufficient" in c

    def test_override_falls_back_when_no_soap_client(self, mongo_db):
        import production_confirmation_service as pcs
        res = pcs.check_component_availability(mongo_db, MATERIAL, 1, SITE, None, REAL_BOM_ID, None)
        comps = [c["product_id"] for c in res["components"]]
        assert any("1.9" in c for c in comps), f"expected cached fallback, got {comps}"

    def test_override_failure_falls_back_to_cached(self, mongo_db):
        import production_confirmation_service as pcs

        class Boom:
            def _fetch_bom_by_id(self, bom_id):
                raise RuntimeError("simulated SAP failure")

        res = pcs.check_component_availability(mongo_db, MATERIAL, 1, SITE, None, REAL_BOM_ID, Boom())
        assert res["checked"] is True
        comps = [c["product_id"] for c in res["components"]]
        assert any("1.9" in c for c in comps), f"expected cached fallback on error, got {comps}"


# ---------- regression: out-of-scope endpoints unchanged ----------
class TestExistingLotAvailabilityUnchanged:
    def test_component_availability_endpoint(self, api, mongo_db):
        import production_confirmation_service as pcs
        r = api.get(
            f"{BASE_URL}/api/production-confirmation/component-availability",
            params={"main_output_product": MATERIAL, "confirmed_quantity": 1, "site_id": SITE},
            timeout=120,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data["checked"] is True
        assert '"_id"' not in r.text and "'_id'" not in r.text
        api_comps = sorted(c["product_id"] for c in data["components"])
        direct = pcs.check_component_availability(mongo_db, MATERIAL, 1, SITE)
        assert api_comps == sorted(c["product_id"] for c in direct["components"])
        assert any("1.9" in c for c in api_comps), f"cached behavior changed: {api_comps}"

    def test_component_availability_missing_param(self, api):
        r = api.get(
            f"{BASE_URL}/api/production-confirmation/component-availability",
            params={"main_output_product": MATERIAL}, timeout=60,
        )
        assert r.status_code == 422


# ---------- regression: create-and-release-order payload contract ----------
class TestCreateAndReleaseContract:
    """Validation-level only - deliberately never lets a request reach SAP
    (real, irreversible production order writes)."""

    def test_accepts_payload_without_production_model_uuid(self, api):
        r = api.post(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order",
            json={"material_id": MATERIAL, "site_id": SITE, "quantity": 1, "unit_code": "EA", "actor": "  "},
            timeout=60,
        )
        # 400 (actor blank) proves the body itself validated fine with the
        # new optional field absent - a 422 would mean the contract broke.
        assert r.status_code == 400, f"{r.status_code}: {r.text[:400]}"
        assert "actor" in r.json()["detail"].lower()

    def test_accepts_payload_with_production_model_uuid(self, api, sos_options):
        opt = next(o for o in sos_options if o["production_model_id"] == REAL_BOM_ID)
        r = api.post(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order",
            json={
                "material_id": MATERIAL, "site_id": SITE, "quantity": 1, "unit_code": "EA", "actor": "",
                "logistic_relationship_uuid": opt["logistic_relationship_uuid"],
                "production_model_uuid": opt["production_model_uuid"],
            },
            timeout=60,
        )
        assert r.status_code == 400, f"{r.status_code}: {r.text[:400]}"

    def test_rejects_bad_availability_datetime(self, api):
        r = api.post(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order",
            json={
                "material_id": MATERIAL, "site_id": SITE, "quantity": 1, "unit_code": "EA",
                "actor": "QA Tester", "availability_datetime": "not-a-date",
            },
            timeout=60,
        )
        assert r.status_code == 400, f"{r.status_code}: {r.text[:400]}"

    def test_rejects_missing_required_fields(self, api):
        r = api.post(
            f"{BASE_URL}/api/production-confirmation/create-and-release-order",
            json={"material_id": MATERIAL}, timeout=60,
        )
        assert r.status_code == 422
