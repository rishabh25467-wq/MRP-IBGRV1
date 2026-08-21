# Unit tests for the NEW site-scoped, warehouse-level per-component 'locations'
# breakdown (Aug 2026): production_confirmation_service._check_availability_against_stock
# must (a) only include stock rows at the TARGET site, and (b) shape each entry as
# {warehouse, stock_status, qty} - no 'site' key.
import sys

import pytest

sys.path.insert(0, "/app/backend")
import production_confirmation_service as pcs  # noqa: E402


BOM_DOC = {
    "_id": "TEST_PARENT",
    "groups": [{
        "items": [
            {"product_id": "COMP_A", "description": "comp A", "unit_of_measure": "EA",
             "quantity": 2.0, "active": True},
            {"product_id": "COMP_B", "description": "comp B", "unit_of_measure": "KGM",
             "quantity": 1.0, "active": True},
            {"product_id": "COMP_UNKNOWN", "description": "no stock data", "unit_of_measure": "EA",
             "quantity": 1.0, "active": True},
        ]
    }],
}

# COMP_A: target site P2 has 2 warehouses, one of which has 2 stock statuses.
# Other sites (P7, P3) must NEVER appear.
STOCK = {
    "COMP_A": [
        {"site": "RADISH TECHNOLOGY-P2", "logistics_area": "P2-WH01", "stock_status": "Unrestricted", "qty": 40.0},
        {"site": "RADISH TECHNOLOGY-P2", "logistics_area": "P2-WH01", "stock_status": "Quality Inspection", "qty": 10.0},
        {"site": "RADISH TECHNOLOGY-P2", "logistics_area": "P2-WH02", "stock_status": "Unrestricted", "qty": 5.0},
        {"site": "RADISH TECHNOLOGY-P7", "logistics_area": "P7-WH01", "stock_status": "Unrestricted", "qty": 999.0},
    ],
    "COMP_B": [
        {"site": "RADISH TECHNOLOGY-P3", "logistics_area": "P3-WH01", "stock_status": "Unrestricted", "qty": 7.5},
    ],
}


@pytest.fixture(scope="module")
def result():
    return pcs._check_availability_against_stock(BOM_DOC, STOCK, confirmed_quantity=10.0, site_id="P2")


def _by_id(res):
    return {c["product_id"]: c for c in res["components"]}


class TestSiteScopedWarehouseLocations:

    def test_checked_and_component_count(self, result):
        assert result["checked"] is True
        assert result["reason"] is None
        assert len(result["components"]) == 3

    def test_only_target_site_rows_included(self, result):
        comp = _by_id(result)["COMP_A"]
        assert len(comp["locations"]) == 3, comp["locations"]
        warehouses = [loc["warehouse"] for loc in comp["locations"]]
        assert warehouses == ["P2-WH01", "P2-WH01", "P2-WH02"]
        assert all("P7" not in (w or "") for w in warehouses)

    def test_entry_shape_has_no_site_key(self, result):
        for comp in result["components"]:
            for loc in comp["locations"]:
                assert set(loc.keys()) == {"warehouse", "stock_status", "qty"}, loc

    def test_stock_status_disambiguates_same_warehouse(self, result):
        locs = _by_id(result)["COMP_A"]["locations"]
        wh01 = [loc for loc in locs if loc["warehouse"] == "P2-WH01"]
        assert len(wh01) == 2
        assert {loc["stock_status"] for loc in wh01} == {"Unrestricted", "Quality Inspection"}
        assert {loc["qty"] for loc in wh01} == {40.0, 10.0}

    def test_available_qty_is_target_site_only(self, result):
        comp = _by_id(result)["COMP_A"]
        assert comp["available_qty"] == 55.0  # 40 + 10 + 5, NOT 1054 including P7
        assert comp["required_qty"] == 20.0
        assert comp["sufficient"] is True

    def test_other_site_only_stock_yields_zero_and_empty_locations(self, result):
        comp = _by_id(result)["COMP_B"]
        assert comp["available_qty"] == 0
        assert comp["locations"] == []
        assert comp["sufficient"] is False

    def test_no_stock_data_stays_unknown(self, result):
        comp = _by_id(result)["COMP_UNKNOWN"]
        assert comp["available_qty"] is None
        assert comp["locations"] == []
        assert comp["sufficient"] is False

    def test_missing_warehouse_or_status_is_none_not_crash(self):
        stock = {"COMP_A": [{"site": "RADISH TECHNOLOGY-P2", "qty": 100.0}]}
        res = pcs._check_availability_against_stock(
            {"groups": [{"items": [{"product_id": "COMP_A", "quantity": 1.0, "active": True}]}]},
            stock, confirmed_quantity=1.0, site_id="P2",
        )
        loc = res["components"][0]["locations"][0]
        assert loc["warehouse"] is None and loc["stock_status"] is None and loc["qty"] == 100.0

    def test_no_bom_doc(self):
        res = pcs._check_availability_against_stock(None, STOCK, 1.0, "P2")
        assert res["checked"] is False and res["components"] == []
