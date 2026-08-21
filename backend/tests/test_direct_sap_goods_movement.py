"""Iteration 102 - DIRECT SAP Goods Movement integration (replaces the
Radish QMS REST wrapper for the Store Approval "issue stock" action).

ALL SAP calls are mocked (either the client's own requests.post, or the
whole SAPGoodsMovementClient.goods_movement) - NO real SAP writes.

Coverage:
  A. SAPGoodsMovementClient wiring: HTTP Basic Auth with SAP_SOAP_USERNAME/
     PASSWORD, endpoint from SAP_SOAP_GOODS_MOVEMENT_ENDPOINT, SOAPAction.
  B. _normalize_logistics_area_id(): "P2/P2-RM" -> "P2-RM".
  C. _resolve_unit_code(): "MASS" -> "KGM", <=3 char passthrough;
     <QuantityTypeCode> keeps the original label.
  D. Live-mode response handling: 200 ok, 401 auth failure, non-200,
     embedded <SeverityCode>3 log error, network exception, qty<=0.
  E. dry_run=True never performs an HTTP call.
  F. store_approval_service._trigger_goods_movement(): 3 attempts w/ 5s
     delay on transient errors, single attempt on "authentication failed",
     never blocks the approval (status still resolved / partial).
  G. No RM stock on file -> {attempted: False, reason: ...}, no SAP call.
  H. server.py wiring: radish_qms_client no longer imported/instantiated;
     oms_client (different system) untouched.
  I. Public API guard (safe: component has no RM stock so SAP is never
     called) - resolve + double-issue rejection.
"""
import os
import sys

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

import sap_goods_movement_client as gm  # noqa: E402
import store_approval_service  # noqa: E402
from sap_goods_movement_client import (  # noqa: E402
    SAPGoodsMovementClient,
    SAPGoodsMovementError,
    _normalize_logistics_area_id,
    _resolve_unit_code,
)

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing from env and /app/frontend/.env")
API = base_url.rstrip("/") + "/api"

TEST_REQUESTER = "TEST_QA_DIRECT_SAP"
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


class FakeResponse:
    def __init__(self, status_code=200, text="<Envelope><ok/></Envelope>"):
        self.status_code = status_code
        self.text = text


class Recorder:
    """Captures the outbound SOAP request instead of sending it to SAP."""

    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse()
        self.exc = exc
        self.calls = []

    def __call__(self, url, data=None, headers=None, auth=None, timeout=None):
        self.calls.append({"url": url, "data": data.decode("utf-8") if isinstance(data, bytes) else data,
                           "headers": headers, "auth": auth, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response


def _client():
    return SAPGoodsMovementClient(
        endpoint=backend_env["SAP_SOAP_GOODS_MOVEMENT_ENDPOINT"],
        username=backend_env["SAP_SOAP_USERNAME"],
        password=backend_env["SAP_SOAP_PASSWORD"],
    )


def _movement(client, **overrides):
    kwargs = {"owner_party_id": "RI", "product_id": "TEST_PROD", "source_logistics_area_id": RM,
              "target_logistics_area_id": SFG, "quantity": 1.5, "quantity_uom": "MASS",
              "site_id": "P2", "dry_run": False}
    kwargs.update(overrides)
    return client.goods_movement(**kwargs)


# --- A. Client wiring / auth / endpoint ------------------------------------
class TestClientWiring:
    def test_env_var_present(self):
        assert backend_env.get("SAP_SOAP_GOODS_MOVEMENT_ENDPOINT")
        assert backend_env.get("SAP_SOAP_USERNAME")
        assert backend_env.get("SAP_SOAP_PASSWORD")

    def test_basic_auth_and_endpoint_used(self, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        result = _movement(_client())
        assert result["ok"] is True
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["url"] == backend_env["SAP_SOAP_GOODS_MOVEMENT_ENDPOINT"]
        assert isinstance(call["auth"], requests.auth.HTTPBasicAuth)
        assert call["auth"].username == backend_env["SAP_SOAP_USERNAME"]
        assert call["auth"].password == backend_env["SAP_SOAP_PASSWORD"]
        assert call["headers"]["SOAPAction"] == gm.SOAP_ACTION
        assert "text/xml" in call["headers"]["Content-Type"]

    def test_same_creds_as_other_sap_soap_calls(self):
        assert backend_env["SAP_SOAP_USERNAME"] == "_EMERGENTBOM"


# --- B. Logistics area normalization --------------------------------------
class TestLogisticsAreaNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("P2/P2-RM", "P2-RM"), ("P2/P2-SFG", "P2-SFG"), ("P8/P8-RM", "P8-RM"),
        ("P2-RM", "P2-RM"), ("A/B/C-RM", "C-RM"),
    ])
    def test_prefix_stripped(self, raw, expected):
        assert _normalize_logistics_area_id(raw) == expected

    def test_envelope_carries_stripped_ids(self, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        _movement(_client())
        xml = rec.calls[0]["data"]
        assert "<SourceLogisticsAreaID>P2-RM</SourceLogisticsAreaID>" in xml
        assert "<TargetLogisticsAreaID>P2-SFG</TargetLogisticsAreaID>" in xml
        assert "P2/P2-RM" not in xml


# --- C. unitCode mapping vs QuantityTypeCode ------------------------------
class TestUnitCodeMapping:
    @pytest.mark.parametrize("label,expected", [
        ("MASS", "KGM"), ("EA", "EA"), ("KGM", "KGM"), ("H87", "H87"),
    ])
    def test_resolve_unit_code(self, label, expected):
        assert _resolve_unit_code(label) == expected

    def test_never_longer_than_three_chars(self):
        for label in ["MASS", "LITRE", "EA", "PIECE", "KGM"]:
            assert len(_resolve_unit_code(label)) <= 3

    def test_envelope_mass_maps_to_kgm_but_type_code_kept(self, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        _movement(_client(), quantity_uom="MASS", quantity=0.001)
        xml = rec.calls[0]["data"]
        assert '<Quantity unitCode="KGM">0.001</Quantity>' in xml
        assert "<QuantityTypeCode>MASS</QuantityTypeCode>" in xml
        assert 'unitCode="MASS"' not in xml

    def test_envelope_ea_passthrough(self, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        _movement(_client(), quantity_uom="EA")
        xml = rec.calls[0]["data"]
        assert 'unitCode="EA"' in xml
        assert "<QuantityTypeCode>EA</QuantityTypeCode>" in xml

    def test_site_product_owner_in_envelope(self, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        _movement(_client(), product_id="P26680", owner_party_id="RT", site_id="P2")
        xml = rec.calls[0]["data"]
        assert "<SiteID>P2</SiteID>" in xml
        assert "<MaterialInternalID>P26680</MaterialInternalID>" in xml
        assert "<OwnerPartyInternalID>RT</OwnerPartyInternalID>" in xml


# --- D. Live-mode response handling ---------------------------------------
class TestResponseHandling:
    def test_ok_response(self, monkeypatch):
        monkeypatch.setattr(gm.requests, "post", Recorder(FakeResponse(200, "<r>done</r>")))
        result = _movement(_client())
        assert result["ok"] is True
        assert result["external_id"].startswith("MOV-")
        assert result["raw_xml"] == "<r>done</r>"

    def test_401_raises_authentication_failed(self, monkeypatch):
        monkeypatch.setattr(gm.requests, "post", Recorder(FakeResponse(401, "denied")))
        with pytest.raises(SAPGoodsMovementError) as e:
            _movement(_client())
        assert "authentication failed" in str(e.value).lower()

    def test_502_html_gateway_body_raises(self, monkeypatch):
        monkeypatch.setattr(gm.requests, "post", Recorder(FakeResponse(502, "<html>502 Bad gateway</html>")))
        with pytest.raises(SAPGoodsMovementError) as e:
            _movement(_client())
        assert "HTTP 502" in str(e.value)

    def test_network_exception_wrapped(self, monkeypatch):
        monkeypatch.setattr(gm.requests, "post",
                            Recorder(exc=requests.exceptions.ConnectTimeout("connect timed out")))
        with pytest.raises(SAPGoodsMovementError) as e:
            _movement(_client())
        assert "unreachable" in str(e.value)

    def test_embedded_severity_error_returns_not_ok(self, monkeypatch):
        xml = ("<Log><Item><SeverityCode>3</SeverityCode>"
               "<Note>Value is longer than maximum permitted length 3</Note></Item></Log>")
        monkeypatch.setattr(gm.requests, "post", Recorder(FakeResponse(200, xml)))
        result = _movement(_client())
        assert result["ok"] is False
        assert "maximum permitted length 3" in result["error"]

    def test_severity_1_info_is_ok(self, monkeypatch):
        xml = "<Log><Item><SeverityCode>1</SeverityCode><Note>All good</Note></Item></Log>"
        monkeypatch.setattr(gm.requests, "post", Recorder(FakeResponse(200, xml)))
        assert _movement(_client())["ok"] is True

    @pytest.mark.parametrize("qty", [0, -1])
    def test_non_positive_quantity_rejected(self, qty, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        with pytest.raises(SAPGoodsMovementError):
            _movement(_client(), quantity=qty)
        assert rec.calls == []


# --- E. dry_run performs no HTTP call -------------------------------------
class TestDryRun:
    def test_dry_run_skips_http(self, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(gm.requests, "post", rec)
        result = _movement(_client(), dry_run=True)
        assert rec.calls == []
        assert result["dry_run"] is True and result["ok"] is True
        assert "<SourceLogisticsAreaID>P2-RM</SourceLogisticsAreaID>" in result["envelope"]


# --- F. Retry logic in _trigger_goods_movement ----------------------------
class StubSap:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def goods_movement(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else self.responses
        if isinstance(item, Exception):
            raise item
        return dict(item)


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(store_approval_service.time, "sleep", lambda s: slept.append(s))
    return slept


class TestRetryLogic:
    def test_transient_error_retried_three_times_with_5s_delay(self, no_sleep):
        stub = StubSap([SAPGoodsMovementError("SAP Goods Movement service unreachable: timeout")] * 3)
        result = store_approval_service._trigger_goods_movement(stub, "RI", "P1", RM, SFG, 1.0, "MASS", "P2")
        assert len(stub.calls) == 3
        assert no_sleep == [5, 5]
        assert result == {"attempted": True, "ok": False,
                          "error": "SAP Goods Movement service unreachable: timeout"}

    def test_succeeds_on_second_attempt(self, no_sleep):
        stub = StubSap([requests.exceptions.ConnectTimeout("boom"), {"ok": True, "external_id": "MOV-1"}])
        result = store_approval_service._trigger_goods_movement(stub, "RI", "P1", RM, SFG, 1.0, "MASS", "P2")
        assert len(stub.calls) == 2
        assert no_sleep == [5]
        assert result["ok"] is True and result["attempted"] is True

    def test_authentication_failure_is_not_retried(self, no_sleep):
        stub = StubSap([SAPGoodsMovementError(
            "SAP SOAP authentication failed for Goods Movement (check SAP_SOAP_USERNAME/PASSWORD).")] * 3)
        result = store_approval_service._trigger_goods_movement(stub, "RI", "P1", RM, SFG, 1.0, "MASS", "P2")
        assert len(stub.calls) == 1
        assert no_sleep == []
        assert result["ok"] is False and "authentication failed" in result["error"]

    def test_dry_run_flag_forwarded_from_env(self, monkeypatch, no_sleep):
        monkeypatch.setenv("RADISH_GOODS_MOVEMENT_DRY_RUN", "false")
        stub = StubSap([{"ok": True}])
        store_approval_service._trigger_goods_movement(stub, "RI", "P1", RM, SFG, 2.0, "MASS", "P2")
        call = stub.calls[0]
        assert call["dry_run"] is False
        assert call["source_logistics_area_id"] == RM  # stripping is the client's job
        assert call["target_logistics_area_id"] == SFG
        assert call["quantity_uom"] == "MASS"
        assert call["owner_party_id"] == "RI"


# --- F2. Approval never blocked by a failing movement ---------------------
def _seed(db, components, job_id="TEST_JOB_QA_DIRECT_SAP"):
    doc = store_approval_service.create_request(
        db, job_id, {"material_id": "TEST_MAT_DS", "site_id": "P2", "quantity": 1.0, "unit_code": "EA"},
        "TEST_PROP_DS", components, TEST_REQUESTER)
    return doc["_id"]


def _comp(product_id, required=1.0, locations=None, uom="MASS"):
    return {"product_id": product_id, "description": "ds comp", "unit_of_measure": uom,
            "required_qty": required, "available_qty": 10.0, "locations": locations or []}


class TestApprovalNeverBlocked:
    def test_full_issue_resolves_even_when_all_three_attempts_fail(self, db, no_sleep):
        rid = _seed(db, [_comp("TEST_DS_FAIL", 1.0, [{"warehouse_id": RM, "owner": "RI", "qty": 5.0}])])
        stub = StubSap([SAPGoodsMovementError("SAP Goods Movement service unreachable: x")] * 3)
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_DS_FAIL", "issued_qty": 1.0}], "proceed", "TEST_STORE", stub)
        assert doc["status"] == "resolved" and doc["resolution"] == "full_issue"
        movement = doc["components"][0]["goods_movement"]
        assert movement["attempted"] is True and movement["ok"] is False
        assert "unreachable" in movement["error"]
        assert len(stub.calls) == 3
        assert doc["issue_id"].startswith("P2-I")

    def test_partial_send_to_planner_resolves_with_failure_recorded(self, db, no_sleep):
        rid = _seed(db, [_comp("TEST_DS_PARTIAL", 10.0, [{"warehouse_id": RM, "owner": "RT", "qty": 4.0}])])
        stub = StubSap([SAPGoodsMovementError("authentication failed")])
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_DS_PARTIAL", "issued_qty": 4.0}], "send_to_planner", "TEST_STORE", stub)
        assert doc["status"] == "partial_pending_planner"
        c = doc["components"][0]
        assert c["shortfall"] == 6.0 and c["issued_qty"] == 4.0
        assert c["goods_movement"]["ok"] is False
        assert len(stub.calls) == 1

    def test_success_path_records_external_id(self, db, no_sleep):
        rid = _seed(db, [_comp("TEST_DS_OK", 2.0, [{"warehouse_id": RM, "owner": "RI", "qty": 5.0}])])
        stub = StubSap([{"ok": True, "external_id": "MOV-ABC123", "raw_xml": "<ok/>"}])
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_DS_OK", "issued_qty": 2.0}], "proceed", "TEST_STORE", stub)
        assert doc["status"] == "resolved"
        c = doc["components"][0]
        assert c["goods_movement"]["ok"] is True
        assert c["goods_movement"]["external_id"] == "MOV-ABC123"
        assert c["issued_from_warehouse"] == RM and c["issued_from_owner"] == "RI"
        assert doc["target_logistics_area_id"] == SFG
        assert stub.calls[0]["quantity"] == 2.0 and stub.calls[0]["site_id"] == "P2"


# --- G. No RM stock on file ----------------------------------------------
class TestNoRmStock:
    def test_no_rm_stock_never_calls_sap(self, db):
        rid = _seed(db, [_comp("TEST_DS_NORM", 1.0, [{"warehouse_id": SFG, "owner": "RI", "qty": 9.0}])])
        stub = StubSap([{"ok": True}])
        doc = store_approval_service.submit_issue(
            db, rid, [{"product_id": "TEST_DS_NORM", "issued_qty": 1.0}], "proceed", "TEST_STORE", stub)
        c = doc["components"][0]
        assert stub.calls == []
        assert c["goods_movement"]["attempted"] is False
        assert "RM warehouse" in c["goods_movement"]["reason"]
        assert c["issued_from_warehouse"] is None


# --- H. server.py wiring / Radish removal --------------------------------
class TestServerWiring:
    SERVER = "/app/backend/server.py"

    def test_radish_qms_not_imported_or_instantiated(self):
        src = open(self.SERVER, encoding="utf-8").read()
        assert "radish_qms_client" not in src
        assert "RadishQMSClient" not in src

    def test_sap_goods_movement_client_instantiated_from_env(self):
        src = open(self.SERVER, encoding="utf-8").read()
        assert "from sap_goods_movement_client import SAPGoodsMovementClient" in src
        assert "os.environ['SAP_SOAP_GOODS_MOVEMENT_ENDPOINT']" in src
        assert "sap_goods_movement_client," in src  # passed into submit_issue

    def test_oms_client_untouched(self):
        src = open(self.SERVER, encoding="utf-8").read()
        assert "oms_client" in src
        assert os.path.exists("/app/backend/oms_client.py")

    def test_store_approval_service_uses_sap_client_param(self):
        src = open("/app/backend/store_approval_service.py", encoding="utf-8").read()
        assert "radish_client" not in src
        assert "def _trigger_goods_movement(sap_client" in src


# --- I. Public API guard (safe - no RM stock so SAP is never called) ------
class TestPublicApi:
    def test_issue_and_double_issue_guard(self, db):
        rid = _seed(db, [_comp("TEST_DS_API", 1.0, [{"warehouse_id": SFG, "owner": "RI", "qty": 5.0}])],
                    job_id="TEST_JOB_QA_DIRECT_SAP_API")
        payload = {"issued": [{"product_id": "TEST_DS_API", "issued_qty": 1.0}],
                   "decision": "proceed", "actor": "TEST_STORE"}
        first = requests.post(f"{API}/store-requests/{rid}/issue", json=payload, timeout=120)
        assert first.status_code == 200, first.text[:400]
        body = first.json()
        assert body["status"] == "resolved"
        assert body["components"][0]["goods_movement"]["attempted"] is False
        assert body["issue_id"].startswith("P2-I")

        second = requests.post(f"{API}/store-requests/{rid}/issue", json=payload, timeout=60)
        assert second.status_code == 400
        assert "no longer pending" in second.text

    def test_missing_actor_rejected(self, db):
        rid = _seed(db, [_comp("TEST_DS_ACTOR", 1.0, [{"warehouse_id": SFG, "owner": "RI", "qty": 5.0}])])
        resp = requests.post(f"{API}/store-requests/{rid}/issue", json={
            "issued": [{"product_id": "TEST_DS_ACTOR", "issued_qty": 1.0}],
            "decision": "proceed", "actor": "   "}, timeout=60)
        assert resp.status_code == 400
        assert "actor" in resp.text

    def test_unknown_request_404(self):
        resp = requests.post(f"{API}/store-requests/TEST_DS_NOPE/issue", json={
            "issued": [], "decision": "proceed", "actor": "TEST_STORE"}, timeout=60)
        assert resp.status_code == 404
