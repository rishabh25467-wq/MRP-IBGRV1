"""Radish QMS Goods-Movement integration (Aug 2026) - store issue flow.

Covers:
- radish_qms_client.RadishQMSClient live dry-run call + session-cookie reuse
- store_approval_service.submit_issue() goods_movement wiring (per component)
- POST /api/store-requests/{id}/issue with & without target_logistics_area_id
"""
import os
import sys
import time

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

TEST_REQUESTER = "TEST_QA_GM"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(os.environ.get("MONGO_URL") or backend_env["MONGO_URL"])
    return client[os.environ.get("DB_NAME") or backend_env["DB_NAME"]]


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module", autouse=True)
def cleanup(db):
    yield
    db["store_requests"].delete_many({"requester": TEST_REQUESTER})


def _seed(db, site="P2"):
    """Two-component pending request: comp A has 2 locations w/ owner,
    comp B has 1 location w/ owner."""
    import store_approval_service
    doc = store_approval_service.create_request(
        db, "TEST_JOB_GM",
        {"material_id": "TEST_GM_MAT", "site_id": site, "quantity": 10.0, "unit_code": "EA"},
        "TEST_PROP_GM",
        [
            {"product_id": "TEST_GM_COMP_A", "description": "comp A", "unit_of_measure": "KGM",
             "required_qty": 20.0, "available_qty": 15.0,
             "locations": [
                 {"warehouse": "RAW MATERIAL GODOWN-P2", "stock_status": "Unrestricted", "qty": 10.0, "owner": "RI"},
                 {"warehouse": "SEMI-FINISH GODOWN-P2", "stock_status": "Not Assigned", "qty": 5.0, "owner": "RT"},
             ]},
            {"product_id": "TEST_GM_COMP_B", "description": "comp B", "unit_of_measure": "EA",
             "required_qty": 5.0, "available_qty": 5.0,
             "locations": [{"warehouse": "RAW MATERIAL GODOWN-P2", "stock_status": "Unrestricted", "qty": 5.0, "owner": "RI"}]},
        ],
        TEST_REQUESTER,
    )
    return doc["_id"]


# --- radish_qms_client -----------------------------------------------------
class TestRadishClient:
    def test_dry_run_goods_movement_live(self):
        from radish_qms_client import RadishQMSClient
        c = RadishQMSClient(backend_env["RADISH_QMS_BASE_URL"], backend_env["RADISH_QMS_EMAIL"], backend_env["RADISH_QMS_PASSWORD"])
        r = c.goods_movement(
            owner_party_id="RI", product_id="TEST_GM_COMP_A",
            source_logistics_area_id="RAW MATERIAL GODOWN-P2", target_logistics_area_id="P2-WIP",
            quantity=3.0, quantity_uom="KGM", site_id="P2", dry_run=True,
        )
        assert r["ok"] is True
        assert r["dry_run"] is True
        assert r["external_id"].startswith("MOV-")
        assert "<TargetLogisticsAreaID>P2-WIP</TargetLogisticsAreaID>" in r["envelope"]
        assert "<OwnerPartyInternalID>RI</OwnerPartyInternalID>" in r["envelope"]

    def test_session_reused_across_calls(self, monkeypatch):
        from radish_qms_client import RadishQMSClient
        c = RadishQMSClient(backend_env["RADISH_QMS_BASE_URL"], backend_env["RADISH_QMS_EMAIL"], backend_env["RADISH_QMS_PASSWORD"])
        calls = {"n": 0}
        orig = RadishQMSClient._login

        def counting_login(self):
            calls["n"] += 1
            return orig(self)

        monkeypatch.setattr(RadishQMSClient, "_login", counting_login)
        args = dict(owner_party_id="RI", product_id="TEST_GM_COMP_A",
                    source_logistics_area_id="RAW MATERIAL GODOWN-P2", target_logistics_area_id="P2-WIP",
                    quantity=1.0, quantity_uom="KGM", site_id="P2", dry_run=True)
        ids = []
        for _ in range(2):
            t = time.time()
            r = c.goods_movement(**args)
            ids.append(r["external_id"])
            print("elapsed", round(time.time() - t, 2))
        assert calls["n"] == 1, f"logged in {calls['n']} times for 2 calls"
        assert "access_token" in c.session.cookies.get_dict()
        assert ids[0] != ids[1]


# --- POST /api/store-requests/{id}/issue -----------------------------------
class TestIssueWithGoodsMovement:
    def test_issue_with_warehouse_and_target_bin(self, db, api):
        rid = _seed(db)
        body = {
            "actor": "QA GM",
            "target_logistics_area_id": "P2-WIP",
            "issued": [
                {"product_id": "TEST_GM_COMP_A", "issued_qty": 20.0,
                 "warehouse": "RAW MATERIAL GODOWN-P2", "owner_party_id": "RI"},
                # comp B: no warehouse/owner -> must NOT fire a movement
                {"product_id": "TEST_GM_COMP_B", "issued_qty": 5.0, "warehouse": None, "owner_party_id": None},
            ],
        }
        resp = api.post(f"{BASE_URL}/api/store-requests/{rid}/issue", json=body, timeout=90)
        assert resp.status_code == 200, resp.text[:500]
        data = resp.json()
        assert data["status"] == "resolved"
        assert data["resolution"] == "full_issue"

        comps = {c["product_id"]: c for c in data["components"]}
        gm = comps["TEST_GM_COMP_A"]["goods_movement"]
        assert gm is not None and gm["attempted"] is True
        assert gm["ok"] is True
        assert gm["dry_run"] is True
        assert gm["external_id"].startswith("MOV-")
        assert "envelope" in gm
        assert comps["TEST_GM_COMP_B"]["goods_movement"] is None

        # persistence: same shape in Mongo
        doc = db["store_requests"].find_one({"_id": rid})
        dcomps = {c["product_id"]: c for c in doc["components"]}
        assert dcomps["TEST_GM_COMP_A"]["goods_movement"]["external_id"] == gm["external_id"]
        assert dcomps["TEST_GM_COMP_A"]["goods_movement"]["dry_run"] is True
        assert dcomps["TEST_GM_COMP_B"]["goods_movement"] is None
        # envelope reflects the picked source + typed target
        env = dcomps["TEST_GM_COMP_A"]["goods_movement"]["envelope"]
        assert "RAW MATERIAL GODOWN-P2" in env and "P2-WIP" in env

    def test_issue_without_target_bin_is_backward_compatible(self, db, api):
        """Regression: target_logistics_area_id omitted entirely."""
        rid = _seed(db)
        body = {
            "actor": "QA GM",
            "issued": [
                {"product_id": "TEST_GM_COMP_A", "issued_qty": 20.0,
                 "warehouse": "RAW MATERIAL GODOWN-P2", "owner_party_id": "RI"},
                {"product_id": "TEST_GM_COMP_B", "issued_qty": 5.0},
            ],
        }
        resp = api.post(f"{BASE_URL}/api/store-requests/{rid}/issue", json=body, timeout=90)
        assert resp.status_code == 200, resp.text[:500]
        data = resp.json()
        assert data["status"] == "resolved"
        for c in data["components"]:
            assert c["goods_movement"] is None
            assert c["issued_qty"] > 0

    def test_issue_partial_with_bin_fires_movement_and_sends_to_planner(self, db, api):
        rid = _seed(db)
        body = {
            "actor": "QA GM",
            "decision": "send_to_planner",
            "target_logistics_area_id": "P2-WIP",
            "issued": [
                {"product_id": "TEST_GM_COMP_A", "issued_qty": 8.0,
                 "warehouse": "SEMI-FINISH GODOWN-P2", "owner_party_id": "RT"},
                {"product_id": "TEST_GM_COMP_B", "issued_qty": 0,
                 "warehouse": "RAW MATERIAL GODOWN-P2", "owner_party_id": "RI"},
            ],
        }
        resp = api.post(f"{BASE_URL}/api/store-requests/{rid}/issue", json=body, timeout=90)
        assert resp.status_code == 200, resp.text[:500]
        data = resp.json()
        assert data["status"] == "partial_pending_planner"
        comps = {c["product_id"]: c for c in data["components"]}
        assert comps["TEST_GM_COMP_A"]["goods_movement"]["ok"] is True
        assert comps["TEST_GM_COMP_A"]["shortfall"] == 12.0
        assert "SEMI-FINISH GODOWN-P2" in comps["TEST_GM_COMP_A"]["goods_movement"]["envelope"]
        assert "<OwnerPartyInternalID>RT</OwnerPartyInternalID>" in comps["TEST_GM_COMP_A"]["goods_movement"]["envelope"]
        # issued_qty == 0 -> no movement even though warehouse/owner/bin given
        assert comps["TEST_GM_COMP_B"]["goods_movement"] is None

    def test_issue_with_bin_but_no_warehouse(self, db, api):
        rid = _seed(db)
        body = {
            "actor": "QA GM",
            "target_logistics_area_id": "P2-WIP",
            "issued": [
                {"product_id": "TEST_GM_COMP_A", "issued_qty": 20.0},
                {"product_id": "TEST_GM_COMP_B", "issued_qty": 5.0},
            ],
        }
        resp = api.post(f"{BASE_URL}/api/store-requests/{rid}/issue", json=body, timeout=90)
        assert resp.status_code == 200, resp.text[:500]
        for c in resp.json()["components"]:
            assert c["goods_movement"] is None

    def test_issue_twice_rejected(self, db, api):
        rid = _seed(db)
        body = {"actor": "QA GM", "issued": [
            {"product_id": "TEST_GM_COMP_A", "issued_qty": 20.0},
            {"product_id": "TEST_GM_COMP_B", "issued_qty": 5.0}]}
        assert api.post(f"{BASE_URL}/api/store-requests/{rid}/issue", json=body, timeout=90).status_code == 200
        second = api.post(f"{BASE_URL}/api/store-requests/{rid}/issue", json=body, timeout=90)
        assert second.status_code == 400
        assert "no longer pending" in second.json()["detail"]

    def test_issue_unknown_request_404(self, api):
        resp = api.post(f"{BASE_URL}/api/store-requests/NOPE-999999/issue",
                        json={"actor": "QA GM", "issued": []}, timeout=60)
        assert resp.status_code == 404

    def test_issue_blank_actor_400(self, db, api):
        rid = _seed(db)
        resp = api.post(f"{BASE_URL}/api/store-requests/{rid}/issue",
                        json={"actor": "   ", "issued": []}, timeout=60)
        assert resp.status_code == 400


# --- availability owner field ---------------------------------------------
def test_availability_locations_carry_owner():
    import production_confirmation_service as pcs
    bom_doc = {"bom_id": "B1", "groups": [{"items": [
        {"product_id": "X1", "description": "d", "unit_of_measure": "KGM", "quantity": 1.0, "active": True},
    ]}]}
    stock = {"X1": [
        {"site": "RADISH-P2", "logistics_area": "RM GODOWN-P2", "stock_status": "Unrestricted",
         "qty": 4.0, "company_code": "RI"},
    ]}
    res = pcs._check_availability_against_stock(bom_doc, stock, 1.0, "P2")
    loc = res["components"][0]["locations"][0]
    assert loc["owner"] == "RI"
    assert loc["warehouse"] == "RM GODOWN-P2"
    assert res["components"][0]["available_qty"] == 4.0
