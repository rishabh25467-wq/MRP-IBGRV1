"""Emergent Managed Object Storage - thin wrapper (Sep 11 2026, user's
explicit ask: "any tool we can build to give u that insight" into
Playwright GRN failures on production). Used to persist failure
screenshots durably so they're viewable from the app itself (and
shareable) regardless of which deployed instance (preview/production)
generated them - a local disk path never was, since each deployment has
its own separate filesystem."""
import logging
import os

import requests

logger = logging.getLogger(__name__)

STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
APP_NAME = "sap-mrp"

_storage_key = None


def init_storage(force: bool = False) -> str:
    global _storage_key
    if _storage_key and not force:
        return _storage_key
    # Read lazily (not at module import time) - this module gets
    # imported before server.py's own load_dotenv() call runs, so a
    # module-level os.environ.get() here would permanently cache None.
    resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": os.environ.get("EMERGENT_LLM_KEY")}, timeout=30)
    resp.raise_for_status()
    _storage_key = resp.json()["storage_key"]
    return _storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data, timeout=60,
    )
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.put(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key, "Content-Type": content_type},
            data=data, timeout=60,
        )
    resp.raise_for_status()
    return resp.json()


def get_object(path: str) -> tuple:
    key = init_storage()
    resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


def upload_failure_screenshot(png_bytes: bytes, po_number: str) -> str | None:
    """Best-effort - a screenshot upload failing must NEVER block returning
    the real GRN failure result to the caller. Returns the storage path
    (not a public URL - always served through our own auth-gated endpoint)
    or None if the upload itself failed."""
    import uuid
    path = f"{APP_NAME}/grn_failures/{po_number}/{uuid.uuid4()}.png"
    try:
        put_object(path, png_bytes, "image/png")
        return path
    except Exception as e:
        logger.warning(f"Could not upload GRN failure screenshot for PO {po_number}: {e}")
        return None
