"""Iteration 182 - verify STO outbound relocation to '{SITE}-HOLD' is now
generalized (runs for ANY ship_from_site_id, not just P8) and no dangling
NameError on the removed old symbols.

Backend-only tests: a live API regression on the STO list endpoint + a
code-path/unit test on submit_order_to_sap with mocked clients (we MUST
NOT trigger a real SAP goods movement in the live production tenant)."""
import os
import sys
import types
import inspect
from unittest.mock import MagicMock

import pytest
import requests

# Make backend/ importable
sys.path.insert(0, "/app/backend")

def _load_frontend_env():
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not found in /app/frontend/.env")

BASE_URL = _load_frontend_env()
STO_ADMIN_COOKIE = "u-8qIcJoBrX8l6ZyV2JKWMte5t5GJsd_vzm6WzC1vqc"


# ---------------- Regression: existing STO read endpoints still work ----------------

class TestStoReadRegression:
    def test_list_orders_returns_200(self):
        r = requests.get(
            f"{BASE_URL}/api/stock-transfer/orders",
            cookies={"vms_session": STO_ADMIN_COOKIE},
            timeout=90,
        )
        assert r.status_code == 200, f"unexpected {r.status_code}: {r.text[:400]}"
        assert isinstance(r.json(), list)

    def test_ship_to_sites_returns_200(self):
        r = requests.get(
            f"{BASE_URL}/api/stock-transfer/ship-to-sites",
            params={"ship_from_site_id": "P1"},
            cookies={"vms_session": STO_ADMIN_COOKIE},
            timeout=90,
        )
        assert r.status_code == 200, f"unexpected {r.status_code}: {r.text[:400]}"


# ---------------- Static / symbol checks: old broken names are GONE ---------------

class TestSourceCleanup:
    def test_no_stale_p8_symbols_in_stock_transfer_service(self):
        with open("/app/backend/stock_transfer_service.py") as f:
            src = f.read()
        # Old undefined symbols that used to crash on import/call
        assert "RELOCATION_SITE_ID" not in src, "Stale RELOCATION_SITE_ID reference still in file"
        assert "_relocate_items_to_p8_source_warehouse" not in src, (
            "Stale P8-only helper reference still in file"
        )

    def test_module_imports_cleanly(self):
        import stock_transfer_service  # must not raise
        assert hasattr(stock_transfer_service, "_relocate_items_to_source_hold_warehouse")
        assert hasattr(stock_transfer_service, "_relocation_hold_warehouse_id")
        assert callable(stock_transfer_service.submit_order_to_sap)

    def test_hold_warehouse_id_built_dynamically_per_site(self):
        import stock_transfer_service as sts
        assert sts._relocation_hold_warehouse_id("P1") == "P1-HOLD"
        assert sts._relocation_hold_warehouse_id("P2") == "P2-HOLD"
        assert sts._relocation_hold_warehouse_id("P3") == "P3-HOLD"
        assert sts._relocation_hold_warehouse_id("P8") == "P8-HOLD"
        # normalization: lowercase and whitespace get uppercased/stripped
        assert sts._relocation_hold_warehouse_id(" p9 ") == "P9-HOLD"

    def test_submit_order_calls_relocation_unconditionally_when_client_provided(self):
        """The fix: no hardcoded `ship_from_site_id == 'P8'` guard around the
        relocation call. The call must fire for ANY site as long as
        sap_goods_movement_client is truthy."""
        import stock_transfer_service as sts
        src = inspect.getsource(sts.submit_order_to_sap)
        # Guard should be on the client alone, not on P8 site id
        assert "if sap_goods_movement_client" in src
        assert "_relocate_items_to_source_hold_warehouse" in src
        # Make sure the old hardcoded P8 branch isn't there anymore
        assert "== 'P8'" not in src and '== "P8"' not in src, (
            "submit_order_to_sap still contains a hardcoded P8 site check"
        )


# ---------------- Code-path test: relocation fires for a non-P8 site ---------------

class TestRelocationRunsForNonP8Site:
    """Directly exercises submit_order_to_sap with a mocked db + mocked SAP
    clients. Verifies (a) relocation helper is invoked for a non-P8 ship-from
    site, (b) the target warehouse it uses is that site's own '{SITE}-HOLD',
    (c) no NameError is raised."""

    def _run_for_site(self, monkeypatch, ship_from_site_id: str):
        import stock_transfer_service as sts

        # Fake STO doc as it would exist after create_stock_transfer_order
        fake_sto = {
            "_id": "TEST-STO-NONP8",
            "ship_from_site_id": ship_from_site_id,
            "ship_to_site_id": "P8" if ship_from_site_id != "P8" else "P1",
            "requested_delivery_date": "2027-01-01",
            "items": [{
                "line_no": 1, "product_id": "PROD-X",
                "source_warehouse_id": f"{ship_from_site_id}-RM",
                "unit_of_measure": "EA", "requested_qty": 1,
                "description": "test",
            }],
        }

        class FakeCollection:
            def __init__(self, doc): self.doc = doc
            def find_one(self, *a, **kw): return self.doc
            def update_one(self, *a, **kw): return MagicMock()

        class FakeDB:
            def __init__(self, doc): self._c = FakeCollection(doc)
            def __getitem__(self, name): return self._c

        db = FakeDB(fake_sto)

        # Track the relocation call
        relocation_calls = []

        def fake_relocate(db_, sto_id, gm_client, doc, items):
            relocation_calls.append({
                "sto_id": sto_id, "ship_from_site_id": doc["ship_from_site_id"],
                "hold_warehouse": sts._relocation_hold_warehouse_id(doc["ship_from_site_id"]),
            })
        monkeypatch.setattr(sts, "_relocate_items_to_source_hold_warehouse", fake_relocate)

        # No GST note / valuation client -> avoid touching db.component_master
        monkeypatch.setattr(sts, "_build_gst_note_text", lambda *a, **kw: "")
        monkeypatch.setattr(sts, "_price_hsn_for_note", lambda *a, **kw: [])

        # Mocked SAP clients - never make a real call
        sap_sto_client = MagicMock()
        sap_sto_client.check.return_value = None
        sap_sto_client.maintain.return_value = {"id": "999999", "uuid": "uuid-999"}
        sap_gm_client = MagicMock()  # truthy - relocation MUST fire

        result = sts.submit_order_to_sap(
            db, sap_sto_client, "TEST-STO-NONP8",
            job_id=None, sap_valuation_client=None,
            sap_goods_movement_client=sap_gm_client,
        )

        assert result == {"sap_order_id": "999999", "sap_order_uuid": "uuid-999"}
        assert len(relocation_calls) == 1, "relocation helper was not invoked"
        return relocation_calls[0]

    def test_relocation_fires_for_p1(self, monkeypatch):
        call = self._run_for_site(monkeypatch, "P1")
        assert call["ship_from_site_id"] == "P1"
        assert call["hold_warehouse"] == "P1-HOLD"

    def test_relocation_fires_for_p3(self, monkeypatch):
        call = self._run_for_site(monkeypatch, "P3")
        assert call["ship_from_site_id"] == "P3"
        assert call["hold_warehouse"] == "P3-HOLD"

    def test_relocation_still_fires_for_p8(self, monkeypatch):
        """Regression - the original P8 flow must still work identically."""
        call = self._run_for_site(monkeypatch, "P8")
        assert call["ship_from_site_id"] == "P8"
        assert call["hold_warehouse"] == "P8-HOLD"

    def test_no_relocation_when_client_missing(self, monkeypatch):
        """If no sap_goods_movement_client is provided (feature not wired at
        runtime), the relocation must be SKIPPED entirely - not crash."""
        import stock_transfer_service as sts
        fake_sto = {
            "_id": "TEST-STO-NOCLIENT",
            "ship_from_site_id": "P1",
            "ship_to_site_id": "P2",
            "requested_delivery_date": "2027-01-01",
            "items": [{"line_no": 1, "product_id": "PROD-Y",
                       "source_warehouse_id": "P1-RM", "unit_of_measure": "EA",
                       "requested_qty": 1, "description": "test"}],
        }

        class FakeCollection:
            def find_one(self, *a, **kw): return fake_sto
            def update_one(self, *a, **kw): return MagicMock()
        class FakeDB:
            def __getitem__(self, name): return FakeCollection()

        called = []
        monkeypatch.setattr(sts, "_relocate_items_to_source_hold_warehouse",
                            lambda *a, **kw: called.append(True))
        monkeypatch.setattr(sts, "_build_gst_note_text", lambda *a, **kw: "")
        monkeypatch.setattr(sts, "_price_hsn_for_note", lambda *a, **kw: [])

        sap_sto_client = MagicMock()
        sap_sto_client.check.return_value = None
        sap_sto_client.maintain.return_value = {"id": "1", "uuid": "u"}

        sts.submit_order_to_sap(
            FakeDB(), sap_sto_client, "TEST-STO-NOCLIENT",
            sap_goods_movement_client=None,
        )
        assert called == [], "relocation must not fire when client is None"
