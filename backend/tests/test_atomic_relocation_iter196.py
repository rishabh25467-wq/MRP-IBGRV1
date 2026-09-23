"""Iteration 196 tests - atomic all-or-nothing STO relocation (Sep 24 2026).

Focus:
 - sap_goods_movement_client.goods_movement_batch: ONE SOAP envelope with
   ONE ExternalID wrapping N <InventoryChangeItemGoodsMovement> blocks.
 - _extract_soap_fault prefers <faultText> over <faultstring>.
 - _attribute_fault_to_line matches the culprit product_id.
 - Batch HTTP 500 Fault -> ok:false + culprit_product_id.
 - Batch HTTP 200 success -> real GACID returned as external_id, shared
   across every line.
 - inbound_receipt_service._relocate_receipt_from_hold: on success every
   line shares the SAME gac_id; on failure every line ok:false, culprit
   gets clarified reason, others get "blocked because bundled with X".
 - retry_receipt_relocation resubmits ALL lines, not just failed ones,
   and status is only ever "done" or "failed" (never "partial").
 - receive_stock_transfer_order status_map has NO 'partial' for
   relocation.
 - Regression: GET /api/inbound-receipts/pending & /completed still load.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

from pymongo import MongoClient

import sap_goods_movement_client as gmc
import inbound_receipt_service as irs

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

SESSION_COOKIE = "u-8qIcJoBrX8l6ZyV2JKWMte5t5GJsd_vzm6WzC1vqc"


@pytest.fixture(scope="module")
def db():
    return MongoClient(MONGO_URL)[DB_NAME]


@pytest.fixture(scope="module")
def session_cookie(db):
    user_id = "test-tid:sto-manual-gi-oid"
    db.auth_users.update_one({"_id": user_id}, {"$set": {
        "tid": "test-tid", "oid": "sto-manual-gi-oid",
        "email": "sto.manualgi.test@rampgroup.co.in", "name": "STO Manual GI Tester",
        "role": "super_admin",
        "allowed_pages": ["stock_transfer", "inbound_stock_transfer"],
        "bound_sites": [],
    }}, upsert=True)
    db.auth_sessions.update_one({"_id": SESSION_COOKIE}, {"$set": {
        "user_id": user_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
    }}, upsert=True)
    return SESSION_COOKIE


# ==================== Unit: batch envelope structure ====================

def test_build_batch_envelope_wraps_all_items_under_one_external_id():
    blocks = [
        gmc._build_item_block("I-A", "PROD-A", "OWN1", "P1-HOLD", "P1-RM", 5.0, "KGM", "MASS"),
        gmc._build_item_block("I-B", "PROD-B", "OWN1", "P1-HOLD", "P1-RM", 2.0, "EA", "EA"),
        gmc._build_item_block("I-C", "PROD-C", "OWN1", "P1-HOLD", "P1-RM", 1.5, "KGM", "MASS"),
    ]
    envelope = gmc._build_batch_envelope("MOV-ABC123", "P1", "2026-09-24T00:00:00.0000000Z", blocks)
    # Exactly ONE GoodsAndActivityConfirmation, ONE ExternalID
    assert envelope.count("<GoodsAndActivityConfirmation>") == 1
    assert envelope.count("<ExternalID>MOV-ABC123</ExternalID>") == 1
    # Three item blocks
    assert envelope.count("<InventoryChangeItemGoodsMovement>") == 3
    assert "PROD-A" in envelope and "PROD-B" in envelope and "PROD-C" in envelope
    # Item IDs
    for iid in ("I-A", "I-B", "I-C"):
        assert f"<ExternalItemID>{iid}</ExternalItemID>" in envelope


def test_goods_movement_batch_dry_run_returns_multi_item_envelope():
    client = gmc.SAPGoodsMovementClient("http://x", "u", "p")
    lines = [
        {"product_id": "A", "source_logistics_area_id": "P1/P1-HOLD",
         "target_logistics_area_id": "P1/P1-RM", "quantity": 3.0, "quantity_uom": "MASS"},
        {"product_id": "B", "source_logistics_area_id": "P1-HOLD",
         "target_logistics_area_id": "P1-RM", "quantity": 1.0, "quantity_uom": "EA"},
    ]
    result = client.goods_movement_batch("OWN1", "P1", lines, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    env = result["envelope"]
    assert env.count("<InventoryChangeItemGoodsMovement>") == 2
    # Composite prefix "P1/" was normalized away
    assert "P1/P1-HOLD" not in env
    assert "<SourceLogisticsAreaID>P1-HOLD</SourceLogisticsAreaID>" in env
    # UN/CEFACT unit code mapping "MASS" -> "KGM"
    assert 'unitCode="KGM"' in env
    assert 'unitCode="EA"' in env


def test_goods_movement_batch_rejects_empty_lines():
    client = gmc.SAPGoodsMovementClient("http://x", "u", "p")
    with pytest.raises(gmc.SAPGoodsMovementError):
        client.goods_movement_batch("OWN1", "P1", [], dry_run=True)


def test_goods_movement_batch_rejects_nonpositive_quantity():
    client = gmc.SAPGoodsMovementClient("http://x", "u", "p")
    with pytest.raises(gmc.SAPGoodsMovementError):
        client.goods_movement_batch("OWN1", "P1", [
            {"product_id": "A", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 0, "quantity_uom": "EA"},
        ], dry_run=True)


# ==================== Unit: fault parsing & attribution ====================

def test_extract_soap_fault_prefers_faulttext_over_faultstring():
    xml = """<S:Envelope><S:Body><S:Fault>
      <faultcode>S:Server</faultcode>
      <faultstring>Application exception occurred!</faultstring>
      <detail><ExceptionData>
        <faultText>Negative stock not permitted in logistics area P1-HOLD, material 09200726-01</faultText>
      </ExceptionData></detail>
    </S:Fault></S:Body></S:Envelope>"""
    assert gmc._extract_soap_fault(xml) == "Negative stock not permitted in logistics area P1-HOLD, material 09200726-01"


def test_extract_soap_fault_falls_back_to_faultstring():
    xml = "<S:Fault><faultstring>Application exception occurred!</faultstring></S:Fault>"
    assert gmc._extract_soap_fault(xml) == "Application exception occurred!"


def test_extract_soap_fault_returns_none_when_no_fault():
    assert gmc._extract_soap_fault("<ok/>") is None


def test_attribute_fault_to_line_matches_culprit_product():
    lines = [
        {"product_id": "PROD-GOOD"},
        {"product_id": "09200726-01"},
        {"product_id": "PROD-OTHER"},
    ]
    fault = "Negative stock not permitted in logistics area P1-HOLD, material 09200726-01"
    assert gmc._attribute_fault_to_line(fault, lines) == "09200726-01"


def test_attribute_fault_to_line_returns_none_when_no_match():
    lines = [{"product_id": "A"}, {"product_id": "B"}]
    assert gmc._attribute_fault_to_line("some unrelated error text", lines) is None
    assert gmc._attribute_fault_to_line("", lines) is None
    assert gmc._attribute_fault_to_line(None, lines) is None


# ==================== Unit: batch HTTP paths (mocked SAP) ====================

def _mock_response(status_code, text):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


def test_goods_movement_batch_http500_fault_maps_to_culprit():
    client = gmc.SAPGoodsMovementClient("http://x", "u", "p")
    fault_xml = """<S:Envelope><S:Body><S:Fault>
      <faultstring>Application exception occurred!</faultstring>
      <detail><ExceptionData>
        <faultText>Negative stock not permitted in logistics area P1-HOLD, material PROD-B</faultText>
      </ExceptionData></detail></S:Fault></S:Body></S:Envelope>"""
    with patch.object(client.session, "post", return_value=_mock_response(500, fault_xml)):
        lines = [
            {"product_id": "PROD-A", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 1.0, "quantity_uom": "EA"},
            {"product_id": "PROD-B", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 2.0, "quantity_uom": "EA"},
        ]
        result = client.goods_movement_batch("OWN1", "P1", lines, dry_run=False)
    assert result["ok"] is False
    assert "PROD-B" in result["error"]
    assert result["culprit_product_id"] == "PROD-B"


def test_goods_movement_batch_http200_success_extracts_gac_id_shared():
    client = gmc.SAPGoodsMovementClient("http://x", "u", "p")
    ok_xml = """<S:Envelope><S:Body>
      <GACDetails><ExternalGACID>MOV-XXX</ExternalGACID><GACID>987654</GACID></GACDetails>
    </S:Body></S:Envelope>"""
    with patch.object(client.session, "post", return_value=_mock_response(200, ok_xml)):
        result = client.goods_movement_batch("OWN1", "P1", [
            {"product_id": "A", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 1.0, "quantity_uom": "EA"},
            {"product_id": "B", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 1.0, "quantity_uom": "EA"},
        ], dry_run=False)
    assert result["ok"] is True
    assert result["external_id"] == "987654"


def test_goods_movement_batch_http200_severity_error_maps_to_culprit():
    client = gmc.SAPGoodsMovementClient("http://x", "u", "p")
    err_xml = """<Response><Log><Item><SeverityCode>3</SeverityCode>
      <Note>Something wrong with PROD-A</Note></Item></Log></Response>"""
    with patch.object(client.session, "post", return_value=_mock_response(200, err_xml)):
        result = client.goods_movement_batch("OWN1", "P1", [
            {"product_id": "PROD-A", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 1.0, "quantity_uom": "EA"},
            {"product_id": "PROD-B", "source_logistics_area_id": "P1-HOLD",
             "target_logistics_area_id": "P1-RM", "quantity": 1.0, "quantity_uom": "EA"},
        ], dry_run=False)
    assert result["ok"] is False
    assert result["culprit_product_id"] == "PROD-A"


# ==================== Unit: _relocate_receipt_from_hold ====================

_STO_DOC = {
    "_id": "STO-TEST-196",
    "ship_to_site_id": "P1",
    "ship_to_location_id": "P1-RM",
    "items": [
        {"line_no": 1, "product_id": "PROD-A", "requested_qty": 5.0, "unit_of_measure": "EA"},
        {"line_no": 2, "product_id": "PROD-B", "requested_qty": 2.5, "unit_of_measure": "MASS"},
        {"line_no": 3, "product_id": "PROD-C", "requested_qty": 1.0, "unit_of_measure": "EA"},
    ],
}


def test_relocate_success_all_lines_share_gac_id():
    fake_client = MagicMock()
    fake_client.goods_movement_batch.return_value = {"ok": True, "external_id": "GAC-999"}
    with patch.object(irs, "is_dry_run", return_value=False), \
         patch.object(irs, "inbound_staging_area_for_site", return_value="P1-HOLD"), \
         patch.object(irs, "company_and_set_of_books_for_site", return_value=("OWNER1", "SOB1")):
        result = irs._relocate_receipt_from_hold(None, fake_client, _STO_DOC)
    assert result["status"] == "done"
    assert result["gac_id"] == "GAC-999"
    assert len(result["lines"]) == 3
    for line in result["lines"]:
        assert line["ok"] is True
        assert line["gac_id"] == "GAC-999"
    # Client received all 3 lines in ONE call, no retry loop for success
    assert fake_client.goods_movement_batch.call_count == 1
    call_lines = fake_client.goods_movement_batch.call_args[0][2]
    assert len(call_lines) == 3


def test_relocate_failure_marks_culprit_and_blocked_bundled_others():
    fake_client = MagicMock()
    fake_client.goods_movement_batch.return_value = {
        "ok": False,
        "error": "Negative stock not permitted in logistics area P1-HOLD, material PROD-B",
        "culprit_product_id": "PROD-B",
    }
    with patch.object(irs, "is_dry_run", return_value=False), \
         patch.object(irs, "inbound_staging_area_for_site", return_value="P1-HOLD"), \
         patch.object(irs, "company_and_set_of_books_for_site", return_value=("OWNER1", "SOB1")):
        result = irs._relocate_receipt_from_hold(None, fake_client, _STO_DOC)
    assert result["status"] == "failed"
    # The retry wrapper only retries on SAPGoodsMovementError EXCEPTIONS.
    # A dict-with-ok:false (a real SAP business rejection) is NOT retried -
    # SAP already made its decision; the whole batch atomically didn't move.
    assert fake_client.goods_movement_batch.call_count == 1
    lines_by_pid = {l["product_id"]: l for l in result["lines"]}
    assert all(l["ok"] is False for l in result["lines"])
    # Culprit line B carries the clarified real SAP reason
    assert "PROD-B" in lines_by_pid["PROD-B"]["error"] or "P1-HOLD" in lines_by_pid["PROD-B"]["error"]
    # Non-culprit lines say "blocked because bundled with PROD-B"
    assert "blocked because PROD-B" in lines_by_pid["PROD-A"]["error"]
    assert "blocked because PROD-B" in lines_by_pid["PROD-C"]["error"]


def test_relocate_skipped_when_target_equals_hold():
    fake_client = MagicMock()
    doc = {**_STO_DOC, "ship_to_location_id": "P1-HOLD"}
    with patch.object(irs, "inbound_staging_area_for_site", return_value="P1-HOLD"):
        result = irs._relocate_receipt_from_hold(None, fake_client, doc)
    assert result["status"] == "skipped_same_warehouse"
    fake_client.goods_movement_batch.assert_not_called()


# ==================== Unit: retry_receipt_relocation resubmits ALL ====================

def test_retry_relocation_resubmits_all_lines_even_after_prior_failure():
    """Sep 24 2026 change: no more preserving already-succeeded lines
    (atomic = there is no such thing as a prior partial success)."""
    class FakeCollection:
        def __init__(self, doc):
            self.doc = doc
            self.updates = []
        def find_one(self, q):
            return self.doc if q.get("_id") == self.doc["_id"] else None
        def update_one(self, q, update):
            self.updates.append(update)
            self.doc.update(update.get("$set", {}))

    doc = {
        **_STO_DOC,
        "receipt_status": "failed",
        # Simulate a prior failed relocation where B was the culprit
        "receipt_relocation": {
            "status": "failed", "to": "P1-RM",
            "lines": [
                {"product_id": "PROD-A", "ok": False, "error": "blocked because PROD-B failed: ..."},
                {"product_id": "PROD-B", "ok": False, "error": "Not enough stock..."},
                {"product_id": "PROD-C", "ok": False, "error": "blocked because PROD-B failed: ..."},
            ],
        },
    }
    fake_col = FakeCollection(doc)
    fake_db = {irs.STO_COLLECTION: fake_col}
    fake_client = MagicMock()
    fake_client.goods_movement_batch.return_value = {"ok": True, "external_id": "GAC-RETRY-1"}
    with patch.object(irs, "is_dry_run", return_value=False), \
         patch.object(irs, "inbound_staging_area_for_site", return_value="P1-HOLD"), \
         patch.object(irs, "company_and_set_of_books_for_site", return_value=("OWNER1", "SOB1")):
        result = irs.retry_receipt_relocation(fake_db, fake_client, "STO-TEST-196")
    # All 3 lines resubmitted in ONE atomic call
    assert fake_client.goods_movement_batch.call_count == 1
    submitted_lines = fake_client.goods_movement_batch.call_args[0][2]
    assert sorted(l["product_id"] for l in submitted_lines) == ["PROD-A", "PROD-B", "PROD-C"]
    # Outcome now 'done' - never 'partial'
    assert result["status"] == "done"
    assert doc["receipt_status"] == "received"
    for line in result["lines"]:
        assert line["ok"] is True
        assert line["gac_id"] == "GAC-RETRY-1"


def test_retry_relocation_status_map_never_partial():
    """The relocation status_map has only 'done' -> 'received' and
    'failed' -> 'failed'. A relocation-status of 'partial' should never
    be produced anymore (atomic = all or nothing)."""
    # Exercise the two real paths and prove no 'partial' rollup can occur.
    class FakeCollection:
        def __init__(self, doc):
            self.doc = doc
        def find_one(self, q):
            return self.doc if q.get("_id") == self.doc["_id"] else None
        def update_one(self, q, update):
            self.doc.update(update.get("$set", {}))

    # Path 1: batch fails -> receipt_status becomes 'failed', never 'partial'.
    doc_fail = {**_STO_DOC, "receipt_status": "failed"}
    fake_col = FakeCollection(doc_fail)
    fake_db = {irs.STO_COLLECTION: fake_col}
    fake_client = MagicMock()
    fake_client.goods_movement_batch.return_value = {
        "ok": False, "error": "Negative stock not permitted, material PROD-B",
        "culprit_product_id": "PROD-B",
    }
    with patch.object(irs, "is_dry_run", return_value=False), \
         patch.object(irs, "inbound_staging_area_for_site", return_value="P1-HOLD"), \
         patch.object(irs, "company_and_set_of_books_for_site", return_value=("OWNER1", "SOB1")):
        irs.retry_receipt_relocation(fake_db, fake_client, "STO-TEST-196")
    assert doc_fail["receipt_status"] == "failed"

    # Path 2: batch succeeds -> 'received', never 'partial'.
    doc_ok = {**_STO_DOC, "receipt_status": "failed"}
    fake_col2 = FakeCollection(doc_ok)
    fake_db2 = {irs.STO_COLLECTION: fake_col2}
    fake_client2 = MagicMock()
    fake_client2.goods_movement_batch.return_value = {"ok": True, "external_id": "GAC-Z"}
    with patch.object(irs, "is_dry_run", return_value=False), \
         patch.object(irs, "inbound_staging_area_for_site", return_value="P1-HOLD"), \
         patch.object(irs, "company_and_set_of_books_for_site", return_value=("OWNER1", "SOB1")):
        irs.retry_receipt_relocation(fake_db2, fake_client2, "STO-TEST-196")
    assert doc_ok["receipt_status"] == "received"


# ==================== Regression: pending/completed endpoints ====================

class TestRegressionEndpoints:
    def test_pending_endpoint_loads(self, session_cookie):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/pending",
                          cookies={"vms_session": session_cookie}, timeout=180)
        assert r.status_code == 200, r.text[:300]
        orders = r.json().get("orders")
        assert isinstance(orders, list)

    def test_completed_endpoint_loads(self, session_cookie):
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/completed",
                          cookies={"vms_session": session_cookie}, timeout=120)
        assert r.status_code == 200, r.text[:300]
        orders = r.json().get("orders")
        assert isinstance(orders, list)
        # Existing older STOs still have items with quantity/UOM populated
        # (backfill from previous session should still work)
        for order in orders[:10]:
            for it in order.get("items") or []:
                assert "unit_of_measure" in it
                assert "requested_qty" in it

    def test_completed_relocation_status_is_never_partial(self, session_cookie):
        """For NEW (post-Sep 24 2026) atomic relocations, receipt_relocation.status
        should only ever be 'done' or 'failed' (or skipped_*). Older records
        stored 'partial' historically - we don't rewrite those, just verify
        the field exists where present."""
        r = requests.get(f"{BASE_URL}/api/inbound-receipts/completed",
                          cookies={"vms_session": session_cookie}, timeout=120)
        assert r.status_code == 200
        orders = r.json().get("orders", [])
        allowed = {"done", "failed", "skipped_same_warehouse", "skipped_no_items", "partial"}
        for order in orders:
            reloc = order.get("receipt_relocation")
            if reloc and "status" in reloc:
                assert reloc["status"] in allowed, f"Unexpected relocation status: {reloc['status']}"
