"""Radish QMS External Drawings API client (Sep 3 2026, user's explicit
ask: "BOM Explorer needs to get latest drawing and any older drawing
from QMS app"). Sister Emergent app, machine-to-machine, read-only - see
https://details-intake-web.preview.emergentagent.com/docs/qms-drawings-api-full.txt

Per that doc's own design principle, this is called ONLY from the
backend (never the browser) so QMS_DRAWINGS_API_KEY stays private -
server.py exposes a thin proxy under /api/bom/qms-drawing/... that the
frontend hits instead.
"""
import httpx


class QMSDrawingsError(Exception):
    pass


class QMSDrawingsClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _get(self, path: str, params: dict = None) -> dict:
        try:
            resp = httpx.get(
                f"{self.base_url}{path}",
                headers={"X-API-Key": self.api_key},
                params=params or {},
                timeout=15.0,
            )
        except httpx.RequestError as e:
            raise QMSDrawingsError(f"QMS Drawings API request failed: {e}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise QMSDrawingsError(f"QMS Drawings API returned {resp.status_code}: {resp.text}")
        return resp.json()

    def get_latest(self, part_no: str, include_history: bool = False) -> dict:
        """None if the part has no published drawing in QMS at all
        (404 - a normal, expected outcome for most parts, not an error)."""
        return self._get(f"/api/external/drawings/by-part/{part_no}", {"include_history": str(include_history).lower()})

    def get_history(self, part_no: str) -> dict:
        return self._get(f"/api/external/drawings/by-part/{part_no}/history")
