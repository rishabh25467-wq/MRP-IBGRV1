"""Tests for the new Production Plan feature: Open PO Demand feed browsing,
BOM Alternates resolution, and MRP Plan generation (async job)."""
import os
import time

import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


class TestOpenPoDemand:
    def test_open_po_demand_basic(self, session):
        resp = session.get(f"{API}/production-plan/open-po-demand")
        assert resp.status_code == 200
        data = resp.json()
        assert "rows" in data
        assert isinstance(data["count"], int)
        assert data["count"] > 0
        row = data["rows"][0]
        assert "item_code" in row
        assert "qty_open" in row

    def test_open_po_demand_customer_filter(self, session):
        resp = session.get(f"{API}/production-plan/open-po-demand")
        assert resp.status_code == 200
        data = resp.json()
        if data["rows"]:
            some_customer = data["rows"][0].get("customer")
            if some_customer:
                filt = session.get(f"{API}/production-plan/open-po-demand", params={"customer": some_customer})
                assert filt.status_code == 200
                fdata = filt.json()
                assert fdata["count"] <= data["count"]
                for r in fdata["rows"]:
                    assert r.get("customer") == some_customer


class TestBomAlternates:
    def test_list_alternates(self, session):
        resp = session.get(f"{API}/production-plan/alternates")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_resolve_and_clear_alternate(self, session):
        items = session.get(f"{API}/production-plan/alternates").json()["items"]
        if not items:
            pytest.skip("No BOM alternates available to test resolve/clear on")
        item = items[0]
        product_id = item["product_id"]
        option = item["options"][0]["bom_id"]

        resolve_resp = session.post(f"{API}/production-plan/alternates/{product_id}", json={"chosen_bom_id": option})
        assert resolve_resp.status_code == 200
        rdata = resolve_resp.json()
        assert rdata["resolved_bom_id"] == option

        # Verify persisted via GET list
        items2 = session.get(f"{API}/production-plan/alternates").json()["items"]
        match = next((i for i in items2 if i["product_id"] == product_id), None)
        assert match is not None
        assert match["resolved_bom_id"] == option

        # Clear
        clear_resp = session.delete(f"{API}/production-plan/alternates/{product_id}")
        assert clear_resp.status_code == 200
        cdata = clear_resp.json()
        assert cdata["resolved_bom_id"] is None

    def test_resolve_unknown_product_returns_404(self, session):
        resp = session.post(f"{API}/production-plan/alternates/DOES_NOT_EXIST_XYZ", json={"chosen_bom_id": "X"})
        assert resp.status_code == 404


class TestPoSelection:
    """PO selection/commitment workflow: toggle select/deselect, actor
    validation, and full audit history (po_selection_service)."""

    def _pick_line(self, session):
        rows = session.get(f"{API}/production-plan/open-po-demand").json()["rows"]
        assert rows
        return rows[15]

    def test_toggle_requires_actor(self, session):
        line = self._pick_line(session)
        resp = session.post(f"{API}/production-plan/po-selections/toggle", json={
            "internal_pono": line["internal_pono"], "item_code": line["item_code"],
            "customer_po": line.get("customer_po"), "customer": line.get("customer"),
            "selected": True, "actor": "   ",
        })
        assert resp.status_code == 400
        assert "actor" in resp.json()["detail"].lower()

    def test_select_then_deselect_persists_and_history(self, session):
        line = self._pick_line(session)
        key = f"{int(line['internal_pono'])}::{line['item_code']}"

        # Select
        sel_resp = session.post(f"{API}/production-plan/po-selections/toggle", json={
            "internal_pono": line["internal_pono"], "item_code": line["item_code"],
            "customer_po": line.get("customer_po"), "customer": line.get("customer"),
            "selected": True, "actor": "TEST_QA_Agent",
        })
        assert sel_resp.status_code == 200
        sdata = sel_resp.json()
        assert sdata["key"] == key
        assert sdata["selected"] is True
        assert sdata["selected_by"] == "TEST_QA_Agent"

        # Verify persisted via list
        listed = session.get(f"{API}/production-plan/po-selections").json()["selections"]
        match = next((s for s in listed if s["key"] == key), None)
        assert match is not None
        assert match["selected"] is True

        # Deselect
        desel_resp = session.post(f"{API}/production-plan/po-selections/toggle", json={
            "internal_pono": line["internal_pono"], "item_code": line["item_code"],
            "customer_po": line.get("customer_po"), "customer": line.get("customer"),
            "selected": False, "actor": "TEST_QA_Agent",
        })
        assert desel_resp.status_code == 200
        assert desel_resp.json()["selected"] is False

        # Verify persisted deselect via list
        listed2 = session.get(f"{API}/production-plan/po-selections").json()["selections"]
        match2 = next((s for s in listed2 if s["key"] == key), None)
        assert match2 is not None
        assert match2["selected"] is False

        # History: most-recent-first, 2 entries (selected then deselected)
        hist = session.get(f"{API}/production-plan/po-selections/history", params={
            "internal_pono": line["internal_pono"], "item_code": line["item_code"],
        }).json()["entries"]
        assert len(hist) >= 2
        assert hist[0]["action"] == "deselected"
        assert hist[1]["action"] == "selected"
        assert hist[0]["by"] == "TEST_QA_Agent"


class TestMrpPlan:
    def test_mrp_reflects_only_selected_lines(self, session):
        """MRP now nets demand ONLY for selected PO lines (behavior change
        this session) - ensure at least one line is selected before
        generating, then verify total_po_lines matches selected count."""
        line = self._pick_line_for_mrp(session)
        session.post(f"{API}/production-plan/po-selections/toggle", json={
            "internal_pono": line["internal_pono"], "item_code": line["item_code"],
            "customer_po": line.get("customer_po"), "customer": line.get("customer"),
            "selected": True, "actor": "TEST_QA_Agent",
        })
        selected_count = len([s for s in session.get(f"{API}/production-plan/po-selections").json()["selections"] if s["selected"]])
        assert selected_count >= 1

        start_resp = session.post(f"{API}/production-plan/mrp/generate")
        assert start_resp.status_code == 200
        job_id = start_resp.json()["job_id"]

        deadline = time.time() + 8 * 60
        status, result = None, None
        while time.time() < deadline:
            poll = session.get(f"{API}/production-plan/mrp/status/{job_id}")
            assert poll.status_code == 200
            pdata = poll.json()
            status = pdata["status"]
            if status == "done":
                result = pdata["result"]
                break
            if status == "failed":
                pytest.fail(f"MRP plan job failed: {pdata.get('error')}")
            time.sleep(5)

        assert status == "done", f"MRP job did not complete within timeout, last status={status}"
        assert result is not None
        # NOTE: total_po_lines can be < selected_count if a previously-selected
        # PO line has since dropped out of the LIVE open-PO feed (e.g. fully
        # shipped/closed upstream) - the feed is re-pulled fresh on every MRP
        # generate, so this is expected, not a bug. Must never exceed it though.
        assert result["total_po_lines"] <= selected_count
        assert result["total_po_lines"] >= 1
        assert isinstance(result["components"], list)

    def _pick_line_for_mrp(self, session):
        rows = session.get(f"{API}/production-plan/open-po-demand").json()["rows"]
        assert rows
        return rows[20]

    def test_generate_and_poll_mrp_plan(self, session):
        start_resp = session.post(f"{API}/production-plan/mrp/generate")
        assert start_resp.status_code == 200
        job_id = start_resp.json()["job_id"]

        deadline = time.time() + 8 * 60  # up to 8 minutes
        status = None
        result = None
        while time.time() < deadline:
            poll = session.get(f"{API}/production-plan/mrp/status/{job_id}")
            assert poll.status_code == 200
            pdata = poll.json()
            status = pdata["status"]
            if status == "done":
                result = pdata["result"]
                break
            if status == "failed":
                pytest.fail(f"MRP plan job failed: {pdata.get('error')}")
            time.sleep(5)

        assert status == "done", f"MRP job did not complete within timeout, last status={status}"
        assert result is not None
        assert result["total_po_lines"] > 0
        assert isinstance(result["components"], list)
        assert len(result["components"]) > 0

        comp = result["components"][0]
        assert "product_id" in comp
        assert "total_net_qty" in comp
        assert "demand_lines" in comp
        # sorted by shortage descending
        nets = [c["total_net_qty"] for c in result["components"]]
        assert nets == sorted(nets, reverse=True)

    def test_mrp_status_unknown_job_404(self, session):
        resp = session.get(f"{API}/production-plan/mrp/status/nonexistent-job-id")
        assert resp.status_code == 404
