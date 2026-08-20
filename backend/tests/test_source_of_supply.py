# Tests for the Source of Supply (Production Model) picker feature
# Endpoint: GET /api/production-confirmation/source-of-supply-options/{material_id}?site_id=
import os
import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
SOS = f"{BASE_URL}/api/production-confirmation/source-of-supply-options"


@pytest.fixture(scope="module")
def client():
    token = open("/tmp/test_token2.txt").read().strip()
    s = requests.Session()
    s.cookies.set("vms_session", token)
    return s


class TestSourceOfSupplyOptions:
    def test_requires_auth(self):
        r = requests.get(f"{SOS}/BK-0021", params={"site_id": "P2"}, timeout=60)
        assert r.status_code in (401, 403), r.text[:300]

    def test_multi_model_material_bk0021_p2(self, client):
        r = client.get(f"{SOS}/BK-0021", params={"site_id": "P2"}, timeout=90)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data.get("material_uuid")
        opts = data["options"]
        assert len(opts) == 2, opts
        ids = sorted(o["production_model_id"] for o in opts)
        assert ids == ["BK-0021_1", "BK-0021_2"], ids
        uuids = {o["logistic_relationship_uuid"] for o in opts}
        assert len(uuids) == 2, "logistic_relationship_uuid must be distinct per model"
        for o in opts:
            assert isinstance(o["logistic_relationship_uuid"], str) and len(o["logistic_relationship_uuid"]) > 10
            assert isinstance(o["is_active"], bool)
        assert any(o["is_active"] for o in opts), "at least one Active option expected"

    def test_single_model_material_htbs_spain_p2(self, client):
        r = client.get(f"{SOS}/HTBS-SPAIN", params={"site_id": "P2"}, timeout=90)
        assert r.status_code == 200, r.text[:500]
        opts = r.json()["options"]
        assert len(opts) <= 1, f"expected 0/1 option at P2, got {opts}"

    def test_site_scoping_changes_result(self, client):
        no_site = client.get(f"{SOS}/BK-0021", timeout=90)
        assert no_site.status_code == 200
        all_opts = no_site.json()["options"]
        p2 = client.get(f"{SOS}/BK-0021", params={"site_id": "P2"}, timeout=90).json()["options"]
        assert len(all_opts) >= len(p2)

    def test_bogus_material_returns_empty_not_error(self, client):
        r = client.get(f"{SOS}/ZZZ-DOES-NOT-EXIST", params={"site_id": "P2"}, timeout=90)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["options"] == []
        assert data["material_uuid"] is None

    def test_bogus_site_handled(self, client):
        r = client.get(f"{SOS}/BK-0021", params={"site_id": "ZZZZ"}, timeout=90)
        # SAP raises "Supply Planning Area not found" -> 502 surfaced; frontend swallows it
        assert r.status_code in (200, 502), r.text[:300]
        if r.status_code == 200:
            assert r.json()["options"] == []


class TestRegressionProductionConfirmation:
    def test_proposal_history(self, client):
        r = client.get(f"{BASE_URL}/api/production-confirmation/proposal-history", timeout=90)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json()["entries"], list)

    def test_reasons_list(self, client):
        r = client.get(f"{BASE_URL}/api/production-confirmation/deviation-reasons", timeout=90)
        assert r.status_code == 200, r.text[:300]

    def test_open_lots(self, client):
        r = client.get(f"{BASE_URL}/api/production-confirmation/open-lots", timeout=180)
        assert r.status_code == 200, r.text[:300]

    def test_create_and_release_validation_missing_actor(self, client):
        r = client.post(f"{BASE_URL}/api/production-confirmation/create-and-release-order", json={
            "material_id": "BK-0021", "site_id": "P2", "quantity": 1, "unit_code": "EA", "actor": "  ",
        }, timeout=90)
        assert r.status_code in (400, 422), r.text[:300]
