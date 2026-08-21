"""Iteration 101 - independent re-verification of this session's Goods
Movement bug fixes, error-path / edge-case focused.

Does NOT duplicate test_gm_warehouse_id_fix.py (iteration 98/100), which
already covers: warehouse_id vs warehouse matching, SeverityCode>=3
downgrade on `raw_xml`, plain exception catch, and a live smoke issue.

New ground covered here:
  A. is_dry_run() lazily reads the CURRENT env value (no import-time cache)
     and dry_run=False is actually forwarded to radish.goods_movement().
  B. RadishQMSError 502/503 never blocks the approval - the request still
     resolves and the failure is recorded on components[].goods_movement.
  C. no-RM-stock-on-file -> {attempted: False, reason: ...} (never a fake
     attempted failure), and issued_from_warehouse stays None.
  D. _has_sap_log_error() also inspects the `envelope` / `raw` response
     keys (dry-run responses carry `envelope`, not `raw_xml`).
  E. partial issue + send_to_planner still fires/records the movement.
  F. issued_qty == 0 components never trigger a movement call at all.
  G. Public API: POST /api/store-requests/{id}/issue on an already-issued
     request is rejected (no double movement).
"""
import os
import sys

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

import store_approval_service  # noqa: E402
from radish_qms_client import RadishQMSError  # noqa: E402

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
API = base_url.rstrip("/") + "/api"

TEST_REQUESTER = "TEST_QA_GM_EDGE"
RM = "P2/P2-RM"
SFG = "P2/P2-SFG"


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
    def __init__(self, response):
        self.response = response
        self.calls = []

    def goods_movement(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return dict(self.response)


def _seed(db, components):
    doc = store_approval_service.create_request(
        db, "TEST_JOB_GM_EDGE",
        {"material_id": "TEST_GM_MAT", "site_id": "P2", "quantity": 1.0, "unit_code": "EA"},
        "TEST_PROP_GM_EDGE", components, TEST_REQUESTER,
    )
    return doc["_id"]


def _comp(product_id, required=1.0, locations=None):
    return {"product_id": product_id, "description": "edge comp", "unit_of_measure": "KGM",
            "required_qty": required, "available_qty": 10.0, "locations": locations or []}


# --- A. is_dry_run() lazy env read -----------------------------------------
class TestDryRunLazyRead:
    def test_reads_current_env_not_import_time_cache(self, monkeypatch):
        monkeypatch.setenv("RADISH_GOODS_MOVEMENT_DRY_RUN", "false")
        assert store_approval_service.is_dry_run() is False
        monkeypatch.setenv("RADISH_GOODS_MOVEMENT_DRY_RUN", "true")
        assert store_approval_service.is_dry_run() is True
        monkeypatch.delenv("RADISH_GOODS_MOVEMENT_DRY_RUN", raising=False)
        assert store_approval_service.is_dry_run() is True  # safe default

    def test_case_insensitive_false(self, monkeypatch):
        monkeypatch.setenv("RADISH_GOODS_MOVEMENT_DRY_RUN", "FALSE")
        assert store_approval_service.is_dry_run() is False

    def test_env_file_has_live_mode_enabled(self):
        assert (backend_env.get("RADISH_GOODS_MOVEMENT_DRY_RUN") or "").strip('"').lower() == "false"

    def test_dry_run_false_is_forwarded_to_client(self, monkeypatch):
        monkeypatch.setenv("RADISH_GOODS_MOVEMENT_DRY_RUN", "false")
        stub = StubRadish({"ok": True, "external_id": "X1"})
        result = store_approval_service._trigger_goods_movement(
            stub, "RI", "P1", RM, SFG, 2.0, "KGM", "P2")
        assert len(stub.calls) == 1
        assert stub.calls[0]["dry_run"] is False
        assert stub.calls[0]["source_logistics_area_id"] == RM
        assert stub.calls[0]["target_logistics_area_id"] == SFG
        assert stub.calls[0]["quantity"] == 2.0
        assert result["attempted"] is True and result["ok"] is True


# --- B. Radish 502/503 never blocks the approval ---------------------------
class TestRadishOutageDoesNotBlockApproval:
    @pytest.mark.parametrize("status", [502, 503])
    def test_trigger_catches_radish_error(self, status):
        stub = StubRadish(RadishQMSError(status, "Radish QMS/SAP unavailable"))
        result = store_approval_service._trigger_goods_movement(
            stub, "RI", "P1", RM, SFG, 1.0, "KGM", "P2")
        assert result["attempted"] is True
        assert result["ok"] is False
        assert "unavailable" in result["error"]

    def test_full_issue_still_resolves_when_radish_is_down(self, db):
        rid = _seed(db, [_comp("TEST_GM_DOWN", 1.0, [{"warehouse_id": RM, "owner": "RI", "qty": 5.0}])])
        stub = StubRadish(RadishQMSError(503, "Radish QMS/SAP unavailable"))
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_GM_DOWN", "issued_qty": 1.0}], "proceed", "TEST_STORE", stub)
        assert doc["status"] == "resolved"
        assert doc["resolution"] == "full_issue"
        c = doc["components"][0]
        assert c["goods_movement"]["attempted"] is True
        assert c["goods_movement"]["ok"] is False
        assert "unavailable" in c["goods_movement"]["error"]
        assert c["issued_from_warehouse"] == RM
        assert c["issued_from_owner"] == "RI"
        assert doc["issue_id"].startswith("P2-I")

    def test_timeout_exception_also_recorded_not_raised(self, db):
        rid = _seed(db, [_comp("TEST_GM_TIMEOUT", 1.0, [{"warehouse_id": RM, "owner": "RT", "qty": 5.0}])])
        stub = StubRadish(requests.exceptions.ConnectTimeout("connect timed out"))
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_GM_TIMEOUT", "issued_qty": 1.0}], "proceed", "TEST_STORE", stub)
        assert doc["status"] == "resolved"
        assert doc["components"][0]["goods_movement"]["ok"] is False
        assert "timed out" in doc["components"][0]["goods_movement"]["error"]


# --- C. No RM stock on file ------------------------------------------------
class TestNoRmStockOnFile:
    def test_stock_only_in_sfg_is_not_attempted(self, db):
        rid = _seed(db, [_comp("TEST_GM_SFGONLY", 1.0, [{"warehouse_id": SFG, "owner": "RI", "qty": 9.0}])])
        stub = StubRadish({"ok": True})
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_GM_SFGONLY", "issued_qty": 1.0}], "proceed", "TEST_STORE", stub)
        c = doc["components"][0]
        assert stub.calls == []
        assert c["goods_movement"]["attempted"] is False
        assert c["goods_movement"]["ok"] is False
        assert "RM warehouse" in c["goods_movement"]["reason"]
        assert c["issued_from_warehouse"] is None
        assert c["issued_from_owner"] is None

    def test_rm_location_without_owner_is_not_attempted(self, db):
        rid = _seed(db, [_comp("TEST_GM_NOOWNER", 1.0, [{"warehouse_id": RM, "qty": 9.0}])])
        stub = StubRadish({"ok": True})
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_GM_NOOWNER", "issued_qty": 1.0}], "proceed", "TEST_STORE", stub)
        assert stub.calls == []
        assert doc["components"][0]["goods_movement"]["attempted"] is False


# --- D. SAP embedded log-error detection on all payload shapes -------------
class TestSapLogErrorPayloadShapes:
    ERR = ("<Log><Item><SeverityCode>3</SeverityCode>"
           "<Note>Source logistics area is invalid</Note></Item></Log>")

    @pytest.mark.parametrize("key", ["raw_xml", "envelope", "raw"])
    def test_detected_on_each_key(self, key):
        assert store_approval_service._has_sap_log_error({key: self.ERR}) == "Source logistics area is invalid"

    def test_severity_9_detected(self):
        assert store_approval_service._has_sap_log_error(
            {"raw_xml": "<Log><Item><SeverityCode>9</SeverityCode></Item></Log>"}) is not None

    def test_empty_and_none_safe(self):
        assert store_approval_service._has_sap_log_error({}) is None
        assert store_approval_service._has_sap_log_error(None) is None

    def test_ok_false_stays_false_and_keeps_original_error(self):
        stub = StubRadish({"ok": False, "error": "original radish error", "raw_xml": self.ERR})
        result = store_approval_service._trigger_goods_movement(stub, "RI", "P1", RM, SFG, 1.0, "KGM", "P2")
        assert result["ok"] is False
        assert result["error"] == "original radish error"


# --- E/F. Partial + zero-issue behaviour ----------------------------------
class TestPartialAndZeroIssue:
    def test_send_to_planner_still_records_movement(self, db):
        rid = _seed(db, [_comp("TEST_GM_PARTIAL", 10.0, [{"warehouse_id": RM, "owner": "RI", "qty": 4.0}])])
        stub = StubRadish({"ok": True, "external_id": "EXT-PART"})
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_GM_PARTIAL", "issued_qty": 4.0}], "send_to_planner", "TEST_STORE", stub)
        assert doc["status"] == "partial_pending_planner"
        assert len(stub.calls) == 1 and stub.calls[0]["quantity"] == 4.0
        c = doc["components"][0]
        assert c["shortfall"] == 6.0
        assert c["goods_movement"]["ok"] is True
        assert c["issued_from_warehouse"] == RM

    def test_zero_issued_never_calls_radish(self, db):
        rid = _seed(db, [_comp("TEST_GM_ZERO", 5.0, [{"warehouse_id": RM, "owner": "RI", "qty": 5.0}])])
        stub = StubRadish({"ok": True})
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_GM_ZERO", "issued_qty": 0}], "send_to_planner", "TEST_STORE", stub)
        assert stub.calls == []
        assert doc["components"][0]["goods_movement"] is None
        assert doc["components"][0]["issued_qty"] == 0
        assert doc["components"][0]["shortfall"] == 5.0


# --- G. Public API guard: no double issue / double movement ----------------
class TestPublicApiGuards:
    def test_double_issue_is_rejected(self, db):
        rid = _seed(db, [_comp("TEST_GM_DOUBLE", 1.0, [{"warehouse_id": SFG, "owner": "RI", "qty": 5.0}])])
        payload = {"issued": [{"product_id": "TEST_GM_DOUBLE", "issued_qty": 1.0}],
                   "decision": "proceed", "actor": "TEST_STORE"}
        first = requests.post(f"{API}/store-requests/{rid}/issue", json=payload, timeout=120)
        assert first.status_code == 200, first.text[:500]
        body = first.json()
        assert body["status"] == "resolved"
        # No RM stock on file -> movement must be skipped, not faked
        assert body["components"][0]["goods_movement"]["attempted"] is False

        second = requests.post(f"{API}/store-requests/{rid}/issue", json=payload, timeout=60)
        assert second.status_code == 400, second.text[:500]
        assert "no longer pending" in second.text

    def test_issue_unknown_request_404(self):
        resp = requests.post(f"{API}/store-requests/TEST_GM_NOPE/issue", json={
            "issued": [], "decision": "proceed", "actor": "TEST_STORE"}, timeout=60)
        assert resp.status_code == 404
