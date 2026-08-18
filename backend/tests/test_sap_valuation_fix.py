"""Tests for SAPValuationClient.get_standard_costs() fix - prefer latest currently-valid
NON-ZERO price across valuation levels for the material '5989829-12' bug.

Bug: material 5989829-12 (product_uuid 03d254d2-8883-1edf-8586-887dd95dd079) has
3 valuation levels (13.45 / 0.00 / 13.45), the $0 one had a later StartDate and was
being picked. Fix now prefers latest-dated currently-valid non-zero price."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import dotenv_values

sys.path.insert(0, "/app/backend")

from sap_valuation_client import SAPValuationClient, _parse_odata_date  # noqa: E402

backend_env = dotenv_values("/app/backend/.env")
SAP_URL = backend_env.get("SAP_ODATA_BASE_URL") or os.environ.get("SAP_ODATA_BASE_URL")
SAP_USER = backend_env.get("SAP_ODATA_USERNAME") or os.environ.get("SAP_ODATA_USERNAME")
SAP_PASS = backend_env.get("SAP_ODATA_PASSWORD") or os.environ.get("SAP_ODATA_PASSWORD")

MASTER_CARTON_UUID = "03d254d2-8883-1edf-8586-887dd95dd079"  # 5989829-12
KNOWN_GOOD_UUID = "a914bea5-2316-1ede-81ed-e5172bb11539"  # P26680, single-valuation reference


@pytest.fixture(scope="module")
def client():
    if not (SAP_URL and SAP_USER and SAP_PASS):
        pytest.skip("SAP OData credentials missing")
    return SAPValuationClient(SAP_URL, SAP_USER, SAP_PASS)


# ----- Unit test with mocked SAP responses (deterministic bug reproduction) -----
class TestNonZeroPreference:
    """Verify selection logic without hitting SAP live."""

    def _make_client_with_fake_data(self, monkeypatch, price_rows_per_level):
        c = SAPValuationClient("http://x", "u", "p")

        def fake_level_ids(uuids):
            return {u.upper(): list(price_rows_per_level.keys()) for u in uuids}

        def fake_prices(level_uuids):
            return {lvl: rows for lvl, rows in price_rows_per_level.items()}

        monkeypatch.setattr(c, "_fetch_valuation_level_ids", fake_level_ids)
        monkeypatch.setattr(c, "_fetch_valuation_prices", fake_prices)
        return c

    def _epoch_ms(self, dt):
        return int(dt.timestamp() * 1000)

    def _price_row(self, amount, start_dt, end_dt=None, level="L1"):
        start = f"/Date({self._epoch_ms(start_dt)})/"
        end = f"/Date({self._epoch_ms(end_dt)})/" if end_dt else f"/Date(253402214400000)/"
        return {"Amount": str(amount), "currencyCode": "INR", "StartDate": start,
                "EndDate": end, "ValuationLevelUUID": level}

    def test_latest_zero_is_skipped_in_favor_of_nonzero(self, monkeypatch):
        """The exact bug: 3 levels, all currently-valid, $0 one has the LATEST StartDate."""
        now = datetime.now(timezone.utc)
        levels = {
            "LEVEL-A": [self._price_row(13.45, now - timedelta(days=100), level="LEVEL-A")],
            "LEVEL-B": [self._price_row(0.00, now - timedelta(days=5), level="LEVEL-B")],  # latest but zero
            "LEVEL-C": [self._price_row(13.45, now - timedelta(days=80), level="LEVEL-C")],
        }
        c = self._make_client_with_fake_data(monkeypatch, levels)
        result = c.get_standard_costs(["FAKE-UUID-1"])
        entry = result["FAKE-UUID-1"]
        assert entry is not None
        assert entry["amount"] == 13.45, f"Expected 13.45 (non-zero preference); got {entry['amount']}"

    def test_all_zero_returns_zero(self, monkeypatch):
        """If genuinely no non-zero price, fall back to zero (not None)."""
        now = datetime.now(timezone.utc)
        levels = {
            "LVL": [self._price_row(0.00, now - timedelta(days=10), level="LVL")],
        }
        c = self._make_client_with_fake_data(monkeypatch, levels)
        result = c.get_standard_costs(["FAKE-UUID-2"])
        entry = result["FAKE-UUID-2"]
        assert entry is not None
        assert entry["amount"] == 0.0

    def test_single_level_unaffected(self, monkeypatch):
        now = datetime.now(timezone.utc)
        levels = {"LVL": [self._price_row(42.5, now - timedelta(days=30), level="LVL")]}
        c = self._make_client_with_fake_data(monkeypatch, levels)
        result = c.get_standard_costs(["FAKE-UUID-3"])
        assert result["FAKE-UUID-3"]["amount"] == 42.5

    def test_latest_nonzero_picked_when_multiple_nonzero(self, monkeypatch):
        """When multiple non-zero prices exist across levels, latest-dated non-zero wins."""
        now = datetime.now(timezone.utc)
        levels = {
            "A": [self._price_row(10.00, now - timedelta(days=100), level="A")],
            "B": [self._price_row(20.00, now - timedelta(days=5), level="B")],
        }
        c = self._make_client_with_fake_data(monkeypatch, levels)
        result = c.get_standard_costs(["FAKE-UUID-4"])
        assert result["FAKE-UUID-4"]["amount"] == 20.00

    def test_expired_prices_ignored(self, monkeypatch):
        now = datetime.now(timezone.utc)
        levels = {
            "A": [self._price_row(99.9, now - timedelta(days=200),
                                   end_dt=now - timedelta(days=10), level="A")],
            "B": [self._price_row(5.0, now - timedelta(days=30), level="B")],
        }
        c = self._make_client_with_fake_data(monkeypatch, levels)
        result = c.get_standard_costs(["FAKE-UUID-5"])
        assert result["FAKE-UUID-5"]["amount"] == 5.0


# ----- Live SAP round-trip (the actual bug material) -----
class TestLiveMasterCartonBug:
    def test_5989829_12_returns_13_45(self, client):
        result = client.get_standard_costs([MASTER_CARTON_UUID])
        key = MASTER_CARTON_UUID.upper()
        assert key in result, f"Missing key in result: {result}"
        entry = result[key]
        assert entry is not None, f"Expected a price entry, got None. Full: {result}"
        assert entry["amount"] == 13.45, (
            f"BUG NOT FIXED: expected 13.45 for master carton, got {entry['amount']}. "
            f"Full entry: {entry}"
        )
        assert entry.get("currency") in ("INR", None) or isinstance(entry.get("currency"), str)

    def test_known_good_material_unchanged(self, client):
        """Regression: single-level material should still return a sane price."""
        result = client.get_standard_costs([KNOWN_GOOD_UUID])
        key = KNOWN_GOOD_UUID.upper()
        assert key in result
        entry = result[key]
        # Not asserting exact value (may change over time), just sanity - either has a price or None
        if entry is not None:
            assert isinstance(entry["amount"], float)
            assert entry["amount"] >= 0

    def test_batch_call_both_materials(self, client):
        result = client.get_standard_costs([MASTER_CARTON_UUID, KNOWN_GOOD_UUID])
        mc = result.get(MASTER_CARTON_UUID.upper())
        assert mc is not None and mc["amount"] == 13.45
