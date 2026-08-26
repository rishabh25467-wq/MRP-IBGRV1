"""Emergent Object Storage wrapper - generic put/get for any file upload
feature in this app. First consumer: Supplier Portal onboarding (GST/PAN
document uploads, Aug 2026).

Reused, not re-implemented, per-feature - any future file upload need in
this app should call put_object()/get_object() here rather than talking
to the storage API directly."""
import logging
import os

import requests

logger = logging.getLogger(__name__)

APP_NAME = "sap-mrp-portal"

_storage_key = None


def _storage_url() -> str:
    # Read lazily (not at module import time) - this module can be
    # imported before server.py's load_dotenv() call runs, which would
    # otherwise silently freeze these at "" / None forever.
    base = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
    return base.rstrip("/") + "/objstore/api/v1/storage"


def init_storage(force: bool = False) -> str:
    """Call once at startup (and lazily again on the first upload/download
    if the cached key ever goes inactive - see the 404-retry in
    put_object/get_object below)."""
    global _storage_key
    if _storage_key and not force:
        return _storage_key
    resp = requests.post(f"{_storage_url()}/init", json={"emergent_key": os.environ.get("EMERGENT_LLM_KEY")}, timeout=30)
    resp.raise_for_status()
    _storage_key = resp.json()["storage_key"]
    return _storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    resp = requests.put(
        f"{_storage_url()}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data, timeout=120,
    )
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.put(
            f"{_storage_url()}/objects/{path}",
            headers={"X-Storage-Key": key, "Content-Type": content_type},
            data=data, timeout=120,
        )
    resp.raise_for_status()
    return resp.json()


def get_object(path: str) -> tuple:
    key = init_storage()
    resp = requests.get(f"{_storage_url()}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.get(f"{_storage_url()}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")
