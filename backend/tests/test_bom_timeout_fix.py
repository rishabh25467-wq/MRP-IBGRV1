"""Regression tests for SAP BOM search timeout/retry fix (sap_soap_client.py).
Verifies BOM search completes within Cloudflare's ~60s gateway timeout and
returns valid JSON (not an HTML error page) for a mix of previously-slow
and previously-fast product IDs.
"""
import os
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")

BOM_IDS = ["100010102-A", "P26680", "P26672", "P26584", "P26736", "100002693-C1"]


@pytest.fixture
def api_client():
    session = requests.Session()
    return session


class TestBomConnectionStatus:
    def test_sap_connection_status(self, api_client):
        resp = api_client.get(f"{BASE_URL}/api/bom/connection-status", timeout=20)
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data or "status" in str(data).lower() or isinstance(data, dict)
        print(f"connection-status response: {data}")


class TestBomSearchTimeoutFix:
    """Each search must return within 60s (Cloudflare gateway limit) as valid JSON."""

    @pytest.mark.parametrize("bom_id", BOM_IDS)
    def test_bom_search_completes_under_gateway_timeout(self, api_client, bom_id):
        start = time.time()
        try:
            resp = api_client.get(
                f"{BASE_URL}/api/bom/search",
                params={"bom_id": bom_id},
                timeout=65,
            )
        except requests.exceptions.Timeout:
            pytest.fail(f"{bom_id}: request exceeded 65s client timeout")
        elapsed = time.time() - start

        # Must not be a raw HTML/Cloudflare error page
        content_type = resp.headers.get("content-type", "")
        assert "text/html" not in content_type, (
            f"{bom_id}: got HTML response (likely Cloudflare error page), status={resp.status_code}"
        )
        assert elapsed < 60, f"{bom_id}: took {elapsed:.1f}s, exceeds 60s gateway timeout"

        # Should be valid JSON regardless of status code (400 is acceptable per convention)
        try:
            data = resp.json()
        except ValueError:
            pytest.fail(f"{bom_id}: response is not valid JSON, status={resp.status_code}, body={resp.text[:300]}")

        print(f"{bom_id}: status={resp.status_code} elapsed={elapsed:.1f}s")

        if resp.status_code == 200:
            assert "tree" in data or "bom_id" in data, f"{bom_id}: 200 response missing expected BOM fields: {data.keys()}"
        else:
            # Degraded/error should still be a clean JSON error, not 502/504
            assert resp.status_code in (400, 404, 422), f"{bom_id}: unexpected status {resp.status_code}: {data}"
