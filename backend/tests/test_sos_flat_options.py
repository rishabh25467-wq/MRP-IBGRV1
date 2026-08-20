# Tests for the REDESIGNED Source of Supply picker (iteration 84):
# get_source_of_supply_options() now returns a FLAT list of (model, site) combos
# with 'description' and 'site_id', unscoped unless site_id is passed.
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


def _assert_shape(opt):
    for key in ("production_model_id", "description", "site_id", "logistic_relationship_uuid", "is_active"):
        assert key in opt, f"missing key {key} in {opt}"
    assert isinstance(opt["production_model_id"], str) and opt["production_model_id"]
    assert isinstance(opt["site_id"], str) and opt["site_id"]
    assert isinstance(opt["logistic_relationship_uuid"], str) and len(opt["logistic_relationship_uuid"]) > 10
    assert isinstance(opt["is_active"], bool)


class TestFlatCombos:
    # HTBS-SPAIN: 2 models on DIFFERENT sites (P2, P3) - must both come back unscoped
    def test_htbs_spain_unscoped_returns_two_sites(self, client):
        r = client.get(f"{SOS}/HTBS-SPAIN", timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data.get("material_uuid")
        opts = data["options"]
        assert len(opts) == 2, opts
        for o in opts:
            _assert_shape(o)
            assert o["description"], "description must be populated for the UI label"
        combos = sorted((o["production_model_id"], o["site_id"]) for o in opts)
        assert combos == [("HTBS-SPAIN_1", "P2"), ("HTBS-SPAIN_2", "P3")], combos
        # sorted by site so the P2 (default) option is first
        assert opts[0]["site_id"] == "P2"

    # BK-0021: genuine SAME-site conflict, both at P2
    def test_bk0021_unscoped_two_models_same_site(self, client):
        r = client.get(f"{SOS}/BK-0021", timeout=120)
        assert r.status_code == 200, r.text[:500]
        opts = r.json()["options"]
        assert len(opts) == 2, opts
        for o in opts:
            _assert_shape(o)
            assert o["site_id"] == "P2", o
            assert o["description"]
        assert sorted(o["production_model_id"] for o in opts) == ["BK-0021_1", "BK-0021_2"]
        assert len({o["logistic_relationship_uuid"] for o in opts}) == 2

    # Single-combo material: picker now shows even with 1 option
    def test_single_combo_material(self, client):
        r = client.get(f"{SOS}/MAZ42117272-TA", timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data.get("material_uuid")
        opts = data["options"]
        assert len(opts) == 1, opts
        _assert_shape(opts[0])
        assert opts[0]["is_active"] is True

    def test_site_filter_still_narrows(self, client):
        p3 = client.get(f"{SOS}/HTBS-SPAIN", params={"site_id": "P3"}, timeout=120)
        assert p3.status_code == 200, p3.text[:500]
        opts = p3.json()["options"]
        assert len(opts) == 1, opts
        assert opts[0]["site_id"] == "P3"
        assert opts[0]["production_model_id"] == "HTBS-SPAIN_2"

    def test_bogus_material_empty(self, client):
        r = client.get(f"{SOS}/ZZZ-NOT-REAL", timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["options"] == []
        assert data.get("material_uuid") in (None, "")

    def test_requires_auth(self):
        r = requests.get(f"{SOS}/HTBS-SPAIN", timeout=60)
        assert r.status_code in (401, 403), r.text[:300]


class TestRegressionEndpoints:
    def test_proposal_history(self, client):
        r = client.get(f"{BASE_URL}/api/production-confirmation/proposal-history", timeout=90)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json()["entries"], list)

    def test_deviation_reasons(self, client):
        r = client.get(f"{BASE_URL}/api/production-confirmation/deviation-reasons", timeout=90)
        assert r.status_code == 200, r.text[:300]

    def test_open_lots(self, client):
        r = client.get(f"{BASE_URL}/api/production-confirmation/open-lots", timeout=240)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json().get("rows"), list)
