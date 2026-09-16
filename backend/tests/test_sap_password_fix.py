"""
Regression tests for SAP dual-account password lockout fix (Sep 16, 2026).

Environment note: python `requests`/`httpx` hang against this preview ingress
in this container (network stack limitation), so we shell out to `curl` which
works reliably (verified manually). Endpoints under test:

SOAP account _EMERGENTBOM (SAP_SOAP_PASSWORD=Emergent@22):
  - GET /api/bom/connection-status
  - GET /api/bom/search?bom_id=<pid>

OData account UNEECOPSTEAM (SAP_ODATA_PASSWORD=UAdmin@11335):
  - GET /api/production-confirmation/source-of-supply-options/<pid>
  - GET /api/inventory
  - GET /api/admin/components/<pid>/sap-planning
"""
import json
import os
import subprocess
import pytest

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://sap-data-sync.preview.emergentagent.com",
).rstrip("/")
COOKIE = "vms_session=8RxdZv3yCcAjffMKxTyZdGLZwOEowMvF_vkAlkiIryA"
PID = "36017845-A"


def _curl(path: str, timeout: int = 60):
    """Return (status_code:int, body:str) using curl."""
    cmd = [
        "curl", "-sS", "-m", str(timeout),
        "-H", f"Cookie: {COOKIE}",
        "-H", "Accept: application/json",
        "-w", "\n__HTTP__%{http_code}",
        f"{BASE_URL}{path}",
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout + 5).decode()
    body, _, code = out.rpartition("__HTTP__")
    return int(code.strip()), body.rstrip("\n")


# ---------- SOAP account (_EMERGENTBOM) ----------
def test_bom_connection_status_soap():
    code, body = _curl("/api/bom/connection-status", timeout=30)
    print("connection-status:", code, body[:300])
    assert code == 200, f"HTTP {code}: {body[:300]}"
    data = json.loads(body)
    assert data.get("connected") is True, f"Not connected: {data}"


def test_bom_search_soap():
    code, body = _curl(f"/api/bom/search?bom_id={PID}", timeout=90)
    print("bom/search:", code, body[:300])
    assert code == 200, f"HTTP {code}: {body[:300]}"
    data = json.loads(body)
    assert data.get("total_components", 0) > 0, f"No BOM components: {data}"
    assert data.get("tree"), "BOM tree empty"
    assert "logon failed" not in body.lower()


# ---------- OData account (UNEECOPSTEAM) ----------
def test_production_source_of_supply_odata():
    code, body = _curl(f"/api/production-confirmation/source-of-supply-options/{PID}", timeout=45)
    print("source-of-supply:", code, body[:400])
    assert code == 200, f"HTTP {code}: {body[:400]}"
    assert "could not verify" not in body.lower()
    assert "logon failed" not in body.lower()
    data = json.loads(body)
    options = data.get("options")
    assert isinstance(options, list) and len(options) > 0, f"No options: {data}"


def test_inventory_odata():
    code, body = _curl("/api/inventory", timeout=45)
    print("inventory:", code, body[:200])
    assert code == 200, f"HTTP {code}: {body[:300]}"
    assert "logon failed" not in body.lower()


def test_admin_sap_planning_odata():
    code, body = _curl(f"/api/admin/components/{PID}/sap-planning", timeout=45)
    print("sap-planning:", code, body[:200])
    assert code == 200, f"HTTP {code}: {body[:300]}"
    data = json.loads(body)
    assert "planning_area_count" in data
    assert "logon failed" not in body.lower()


# ---------- Env sanity ----------
def test_env_passwords_present():
    env_path = "/app/backend/.env"
    assert os.path.exists(env_path)
    with open(env_path) as f:
        content = f.read()
    assert 'SAP_SOAP_PASSWORD="Emergent@22"' in content, "SOAP password mismatch"
    assert 'SAP_ODATA_PASSWORD="UAdmin@11335"' in content, "OData password mismatch"
