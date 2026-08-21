"""Aug 2026 bug: "approved on /storeapproval but stock never moved".

Covers the two fixes:
1. store_approval_service.submit_issue() must match the fixed RM warehouse
   against location["warehouse_id"] (raw SAP Logistics Area ID, "P2/P2-RM"),
   NOT location["warehouse"] (human description "RAW MATERIAL GODOWN-P2").
2. store_approval_service._has_sap_log_error() / _trigger_goods_movement()
   must downgrade a Radish `ok: true` response whose raw XML embeds a SAP
   <Log> item with SeverityCode >= 3.

Also asserts the field plumbing (sap_inventory_client -> inventory_service ->
production_confirmation_service) carries logistics_area_id/warehouse_id, and
does a light live end-to-end issue through the public API.
"""
import os
import sys

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
API = f"{BASE_URL}/api"

TEST_REQUESTER = "TEST_QA_WHID"

SAP_LOG_ERROR_XML = (
    "<GoodsAndActivityConfirmationGoodsMovementResponse><Log><Item>"
    "<TypeID>001(SLS_CORE)</TypeID><SeverityCode>3</SeverityCode>"
    "<Note>Source logistics area is invalid; check your entry</Note>"
    "</Item></Log></GoodsAndActivityConfirmationGoodsMovementResponse>"
)


@pytest.fixture(scope="module")
def db():
    client = MongoClient(os.environ.get("MONGO_URL") or backend_env["MONGO_URL"])
    yield client[os.environ.get("DB_NAME") or backend_env["DB_NAME"]]
    client.close()


@pytest.fixture(scope="module", autouse=True)
def cleanup(db):
    yield
    db["store_requests"].delete_many({"requester": TEST_REQUESTER})


class StubRadish:
    """Canned Radish client - records calls, returns a scripted response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def goods_movement(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return dict(self.response)


def _seed(db, site="P2", locations=None, product="TEST_WHID_COMP", required=0.01):
    import store_approval_service
    doc = store_approval_service.create_request(
        db, "TEST_JOB_WHID",
        {"material_id": "TEST_WHID_MAT", "site_id": site, "quantity": 1.0, "unit_code": "EA"},
        "TEST_PROP_WHID",
        [{"product_id": product, "description": "whid comp", "unit_of_measure": "KGM",
          "required_qty": required, "available_qty": 5.0, "locations": locations or []}],
        TEST_REQUESTER,
    )
    return doc["_id"]


REAL_SHAPE_LOCATIONS = [
    {"warehouse": "SEMI-FINISH GODOWN-P2", "warehouse_id": "P2/P2-SFG",
     "stock_status": "Unrestricted", "qty": 5.0, "owner": "RT"},
    {"warehouse": "RAW MATERIAL GODOWN-P2", "warehouse_id": "P2/P2-RM",
     "stock_status": "Unrestricted", "qty": 10.0, "owner": "RI"},
]


# --- Fix 1: warehouse_id matching in submit_issue --------------------------
class TestWarehouseIdMatching:
    def test_rm_location_selected_by_warehouse_id(self, db):
        import store_approval_service
        rid = _seed(db, locations=REAL_SHAPE_LOCATIONS)
        stub = StubRadish({"ok": True, "dry_run": False, "external_id": "MOV-STUB-1"})
        updated = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_WHID_COMP", "issued_qty": 0.01}], None, "QA WHID", stub)

        c = updated["components"][0]
        assert c["goods_movement"] is not None, "no movement fired - the reported bug"
        assert c["goods_movement"]["attempted"] is True
        assert c["issued_from_warehouse"] == "P2/P2-RM"
        assert c["issued_from_owner"] == "RI", "must use the RM location's owner, not SFG's"

        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["source_logistics_area_id"] == "P2/P2-RM"
        assert call["target_logistics_area_id"] == "P2/P2-SFG"
        assert call["owner_party_id"] == "RI"
        assert call["quantity"] == 0.01
        assert call["site_id"] == "P2"
        assert call["quantity_uom"] == "KGM"
        # iteration_101: module-level constant replaced by lazy is_dry_run()
        assert call["dry_run"] == store_approval_service.is_dry_run()

        # persisted
        doc = db["store_requests"].find_one({"_id": rid})
        assert doc["components"][0]["goods_movement"]["external_id"] == "MOV-STUB-1"
        assert doc["components"][0]["issued_from_warehouse"] == "P2/P2-RM"
        assert doc["status"] == "resolved"

    def test_no_rm_stock_means_no_movement(self, db):
        """Only SFG stock on file -> movement legitimately skipped."""
        import store_approval_service
        rid = _seed(db, locations=[REAL_SHAPE_LOCATIONS[0]])
        stub = StubRadish({"ok": True})
        updated = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_WHID_COMP", "issued_qty": 0.01}], None, "QA WHID", stub)
        # iteration_101: skipped movements now carry an explicit marker
        gm = updated["components"][0]["goods_movement"]
        assert gm["attempted"] is False and "RM warehouse" in gm["reason"]
        assert stub.calls == []

    def test_legacy_doc_without_warehouse_id_does_not_move(self, db):
        """Docs created BEFORE this fix have no warehouse_id -> no match.
        Documents behaviour for pre-existing pending requests."""
        import store_approval_service
        rid = _seed(db, locations=[{"warehouse": "RAW MATERIAL GODOWN-P2", "qty": 10.0, "owner": "RI"}])
        stub = StubRadish({"ok": True})
        updated = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_WHID_COMP", "issued_qty": 0.01}], None, "QA WHID", stub)
        gm = updated["components"][0]["goods_movement"]
        assert gm["attempted"] is False and "RM warehouse" in gm["reason"]
        assert stub.calls == []


# --- Fix 2: SAP <Log> SeverityCode>=3 detection ----------------------------
class TestSapLogErrorDetection:
    def test_has_sap_log_error_detects_severity_3(self):
        import store_approval_service
        for key in ("raw_xml", "envelope", "raw"):
            msg = store_approval_service._has_sap_log_error({"ok": True, key: SAP_LOG_ERROR_XML})
            assert msg and "Source logistics area is invalid" in msg, key

    def test_has_sap_log_error_ignores_info_severity(self):
        import store_approval_service
        xml = "<Log><Item><SeverityCode>1</SeverityCode><Note>All good</Note></Item></Log>"
        assert store_approval_service._has_sap_log_error({"ok": True, "raw_xml": xml}) is None
        assert store_approval_service._has_sap_log_error({"ok": True}) is None
        assert store_approval_service._has_sap_log_error({}) is None

    def test_trigger_downgrades_ok_true_with_sap_error(self):
        import store_approval_service
        stub = StubRadish({"ok": True, "faults": [], "raw_xml": SAP_LOG_ERROR_XML})
        r = store_approval_service._trigger_goods_movement(
            stub, "RI", "P", "P2/P2-RM", "P2/P2-SFG", 0.01, "KGM", "P2")
        assert r["attempted"] is True
        assert r["ok"] is False
        assert "Source logistics area is invalid" in r["error"]

    def test_trigger_keeps_ok_true_when_clean(self):
        import store_approval_service
        stub = StubRadish({"ok": True, "faults": [], "raw_xml": "<Log/>"})
        r = store_approval_service._trigger_goods_movement(
            stub, "RI", "P", "P2/P2-RM", "P2/P2-SFG", 0.01, "KGM", "P2")
        assert r["ok"] is True and r["attempted"] is True

    def test_trigger_handles_client_exception(self):
        import store_approval_service
        stub = StubRadish(requests.ConnectionError("boom 502"))
        r = store_approval_service._trigger_goods_movement(
            stub, "RI", "P", "P2/P2-RM", "P2/P2-SFG", 0.01, "KGM", "P2")
        assert r == {"attempted": True, "ok": False, "error": "boom 502"} or (
            r["attempted"] is True and r["ok"] is False and "boom 502" in r["error"])

    def test_end_to_end_downgrade_visible_on_component(self, db):
        import store_approval_service
        rid = _seed(db, locations=REAL_SHAPE_LOCATIONS)
        stub = StubRadish({"ok": True, "faults": [], "raw_xml": SAP_LOG_ERROR_XML})
        updated = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_WHID_COMP", "issued_qty": 0.01}], None, "QA WHID", stub)
        gm = updated["components"][0]["goods_movement"]
        assert gm["attempted"] is True and gm["ok"] is False
        assert "Source logistics area is invalid" in gm["error"]


# --- Field plumbing --------------------------------------------------------
class TestPlumbing:
    def test_check_availability_against_stock_emits_warehouse_id(self):
        import production_confirmation_service as pcs
        bom = {"bom_id": "B1", "groups": [{"items": [
            {"product_id": "C1", "description": "c1", "unit_of_measure": "KGM", "quantity": 1.0, "active": True}]}]}
        stock = {"C1": [
            {"site": "RADISH TECHNOLOGY-P2", "logistics_area": "RAW MATERIAL GODOWN-P2",
             "logistics_area_id": "P2/P2-RM", "stock_status": "Unrestricted", "qty": 7.0,
             "company_code": "RI"}]}
        out = pcs._check_availability_against_stock(bom, stock, 1.0, "P2")
        loc = out["components"][0]["locations"][0]
        assert loc["warehouse_id"] == "P2/P2-RM"
        assert loc["warehouse"] == "RAW MATERIAL GODOWN-P2"
        assert loc["owner"] == "RI"

    def test_live_inventory_exposes_logistics_area_id(self):
        """The real check: does live SAP data actually contain the
        '{site}/{site}-RM' ID shape submit_issue() matches on?"""
        from sap_inventory_client import SAPInventoryClient, SAPInventoryError
        inst = SAPInventoryClient(
            report_url=backend_env["SAP_INVENTORY_ODATA_URL"],
            username=backend_env["SAP_ODATA_USERNAME"],
            password=backend_env["SAP_ODATA_PASSWORD"],
        )
        try:
            rows = inst.get_inventory_detail()
        except (SAPInventoryError, requests.RequestException) as e:
            pytest.skip(f"live SAP inventory report unavailable: {e}")
        ids = sorted({r.get("logistics_area_id") for r in rows if r.get("logistics_area_id")})
        print("distinct logistics_area_id values:", ids[:40])
        assert ids, "no logistics_area_id present in live inventory rows"
        rm_ids = [i for i in ids if i.endswith("-RM")]
        print("RM-looking ids:", rm_ids)
        assert any(i == "P2/P2-RM" for i in ids) or rm_ids, (
            f"no RM logistics area id in live data; submit_issue matches on '{{site}}/{{site}}-RM'. Got: {ids[:20]}")


# --- Light regression smoke on public endpoints ----------------------------
class TestSmoke:
    def test_list_and_journal(self):
        for path in ("/store-requests", "/store-requests/journal"):
            r = requests.get(f"{API}{path}", timeout=60)
            assert r.status_code == 200, r.text[:300]
            assert isinstance(r.json().get("requests"), list)

    def test_live_issue_via_api_attempts_movement(self, db):
        """LIVE Radish call with tiny qty. Expect attempted=True; ok may be
        False (SAP logistics-area-ID format is a known open item)."""
        rid = _seed(db, locations=REAL_SHAPE_LOCATIONS, required=0.01)
        r = requests.post(f"{API}/store-requests/{rid}/issue", timeout=180,
                          json={"actor": "QA WHID", "issued": [
                              {"product_id": "TEST_WHID_COMP", "issued_qty": 0.01}]})
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        c = data["components"][0]
        gm = c["goods_movement"]
        print("LIVE goods_movement:", {k: v for k, v in (gm or {}).items() if k != "envelope"})
        assert gm is not None and gm["attempted"] is True, "movement never attempted despite RM stock on file"
        assert c["issued_from_warehouse"] == "P2/P2-RM"
        assert c["issued_from_owner"] == "RI"
        if gm.get("ok"):
            print("LIVE movement reported ok:true")
        else:
            assert gm.get("error"), "failed movement must carry an error reason"


# --- Env flag plumbing (BUG found this round) ------------------------------
class TestDryRunFlagPlumbing:
    def test_env_false_is_honoured_by_running_server(self, db):
        """backend/.env has RADISH_GOODS_MOVEMENT_DRY_RUN="false" (LIVE).
        server.py imports store_approval_service (line ~59) BEFORE
        load_dotenv() (line ~66), so the module-level constant is computed
        from an env that does not yet contain the var -> defaults to
        dry-run True and the LIVE flip silently has no effect."""
        assert backend_env.get("RADISH_GOODS_MOVEMENT_DRY_RUN", "").strip('"').lower() == "false", \
            "test assumes .env is set to LIVE"
        rid = _seed(db, locations=REAL_SHAPE_LOCATIONS)
        r = requests.post(f"{API}/store-requests/{rid}/issue", timeout=180,
                          json={"actor": "QA WHID", "issued": [
                              {"product_id": "TEST_WHID_COMP", "issued_qty": 0.01}]})
        assert r.status_code == 200, r.text[:300]
        gm = r.json()["components"][0]["goods_movement"]
        print("server-side live gm keys:", sorted(gm.keys()), {k: v for k, v in gm.items() if k not in ("envelope", "raw_xml")})
        # iteration_101: Radish's response does not echo `dry_run`, so the
        # server-side proof is the absence of the dry-run-only `envelope`
        # key (radish_qms_client docstring: "on dry runs `envelope`").
        assert gm["attempted"] is True
        assert "envelope" not in gm, (
            "server still ran the movement in DRY-RUN despite .env=false "
            "(import order: store_approval_service imported before load_dotenv)")


class TestLiveMovementWithFlagForced:
    """Bypasses the broken env plumbing in-process to prove what a real
    LIVE movement returns post-fix (attempted + real SAP outcome)."""

    def test_live_movement_against_real_rm_stock(self, db, monkeypatch):
        import store_approval_service
        from radish_qms_client import RadishQMSClient
        from sap_inventory_client import SAPInventoryClient, SAPInventoryError
        inv = SAPInventoryClient(
            report_url=backend_env["SAP_INVENTORY_ODATA_URL"],
            username=backend_env["SAP_ODATA_USERNAME"],
            password=backend_env["SAP_ODATA_PASSWORD"],
        )
        try:
            rows = inv.get_inventory_detail()
        except (SAPInventoryError, requests.RequestException) as e:
            pytest.skip(f"live inventory unavailable: {e}")
        candidates = [r for r in rows if r.get("logistics_area_id") == "P2/P2-RM"
                      and (r.get("qty") or 0) > 1 and r.get("company_code")]
        if not candidates:
            pytest.skip("no real P2/P2-RM stock row found")
        row = candidates[0]
        print("using real stock row:", row["product_id"], row["company_code"], row["qty"], row.get("uom"))
        locs = [{"warehouse": row.get("logistics_area"), "warehouse_id": "P2/P2-RM",
                 "stock_status": row.get("stock_status"), "qty": row["qty"],
                 "owner": row["company_code"]}]
        rid = _seed(db, locations=locs, product=row["product_id"])
        monkeypatch.setenv("RADISH_GOODS_MOVEMENT_DRY_RUN", "false")
        client = RadishQMSClient(backend_env["RADISH_QMS_BASE_URL"], backend_env["RADISH_QMS_EMAIL"], backend_env["RADISH_QMS_PASSWORD"])
        updated = store_approval_service.submit_issue(
            db, rid, [{"product_id": row["product_id"], "issued_qty": 0.01}], None, "QA WHID LIVE", client)
        gm = updated["components"][0]["goods_movement"]
        print("LIVE (forced) movement:", {k: v for k, v in gm.items() if k not in ("envelope",)})
        assert gm is not None and gm["attempted"] is True
        assert gm.get("dry_run") is not True
        assert updated["components"][0]["issued_from_warehouse"] == "P2/P2-RM"
        if not gm.get("ok"):
            assert gm.get("error"), "failed live movement must carry an error reason"
